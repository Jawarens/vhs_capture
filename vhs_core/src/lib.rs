/*!
vhs_core — native Rust hot-path for VHS Capture
================================================

Exposed to Python via PyO3:

  detect_blue(buf: bytes, width: int, height: int,
              threshold: float, sample_w: int, sample_h: int) -> float
      Downsample a BGR frame and return the fraction of pixels that fall in
      the VHS-blue hue range (190–265°, sat > 0.20, blue channel > 60).
      Zero-copy: reads directly from the bytes Python already owns.

  bgr_to_rgb(buf: bytes) -> bytes
      Swap R and B channels in-place on a copy.  Much faster than
      cv2.cvtColor called from Python because there is no GIL round-trip
      and the loop auto-vectorises with SIMD.

  FrameQueue (class)
      A bounded, lock-free-style queue for raw BGR frames.
      push(buf: bytes) -> bool      — non-blocking; returns False if full
      pop()            -> bytes|None — returns next frame or None if empty
      clear()                        — drain everything
      __len__()        -> int
*/

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

// ─────────────────────────────────────────────────────────────────────────────
//  Blue detection
// ─────────────────────────────────────────────────────────────────────────────

/// Convert BGR pixel to hue in degrees [0, 360).
/// Returns -1.0 if the pixel is achromatic (sat == 0).
#[inline(always)]
fn bgr_to_hue(b: u8, g: u8, r: u8) -> f32 {
    let rf = r as f32 / 255.0;
    let gf = g as f32 / 255.0;
    let bf = b as f32 / 255.0;

    let max = rf.max(gf).max(bf);
    let min = rf.min(gf).min(bf);
    let delta = max - min;

    if delta < 1e-6 {
        return -1.0; // achromatic
    }

    let hue = if max == rf {
        60.0 * (((gf - bf) / delta) % 6.0)
    } else if max == gf {
        60.0 * ((bf - rf) / delta + 2.0)
    } else {
        60.0 * ((rf - gf) / delta + 4.0)
    };

    if hue < 0.0 { hue + 360.0 } else { hue }
}

/// Nearest-neighbour downsample a BGR frame and count blue pixels.
/// Returns the fraction [0.0, 1.0] of sampled pixels that are "VHS blue".
///
/// A pixel is blue when:
///   • hue in [190°, 265°]   (blue-to-indigo arc, catches blue-screen variants)
///   • saturation > 0.20     (not grey/white noise)
///   • blue channel > 60     (not dark/black)
#[pyfunction]
fn detect_blue(
    py: Python<'_>,
    buf: &[u8],
    width: usize,
    height: usize,
    threshold: f32,   // kept for future use; caller can use raw ratio instead
    sample_w: usize,
    sample_h: usize,
) -> PyResult<f32> {
    // Release the GIL for the entire computation — this is the key win.
    // Python threads (preview emit, UI) can run freely while we crunch pixels.
    let ratio = py.allow_threads(|| {
        let x_step = width  as f32 / sample_w as f32;
        let y_step = height as f32 / sample_h as f32;
        let stride = width * 3; // BGR, no padding assumed

        let mut blue_count: u32 = 0;
        let total = (sample_w * sample_h) as f32;

        for sy in 0..sample_h {
            let src_y = (sy as f32 * y_step) as usize;
            let row_base = src_y * stride;

            for sx in 0..sample_w {
                let src_x = (sx as f32 * x_step) as usize;
                let i = row_base + src_x * 3;

                if i + 2 >= buf.len() {
                    continue;
                }

                let b = buf[i];
                let g = buf[i + 1];
                let r = buf[i + 2];

                // Fast early-outs before calling the full hue fn
                if b < 60 {
                    continue; // blue channel too dark
                }

                let max = r.max(g).max(b);
                let min = r.min(g).min(b);
                let delta = (max - min) as f32;
                let sat = delta / max as f32;

                if sat < 0.20 {
                    continue; // too grey
                }

                let hue = bgr_to_hue(b, g, r);
                if hue >= 190.0 && hue <= 265.0 {
                    blue_count += 1;
                }
            }
        }

        blue_count as f32 / total
    });

    Ok(ratio)
}

// ─────────────────────────────────────────────────────────────────────────────
//  BGR → RGB channel swap
// ─────────────────────────────────────────────────────────────────────────────

/// Swap R and B channels.  The compiler auto-vectorises this loop with SIMD
/// (SSE2/AVX2 on x86, NEON on ARM) at -O3, giving ~8-16 pixels per cycle.
#[pyfunction]
fn bgr_to_rgb<'py>(py: Python<'py>, buf: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
    let mut out = buf.to_vec();
    py.allow_threads(|| {
        for chunk in out.chunks_exact_mut(3) {
            chunk.swap(0, 2); // B↔R
        }
    });
    Ok(PyBytes::new_bound(py, &out))
}

// ─────────────────────────────────────────────────────────────────────────────
//  FrameQueue — bounded queue for raw BGR frames
// ─────────────────────────────────────────────────────────────────────────────

/// Bounded queue of raw BGR frame buffers.
/// Python sees it as an opaque object with push/pop/clear/__len__.
///
/// Uses a Mutex<VecDeque> which is slightly heavier than a true lock-free
/// structure, but VecDeque gives O(1) push/pop and the lock is held for
/// microseconds — far below any frame interval.
#[pyclass]
struct FrameQueue {
    inner: Arc<Mutex<VecDeque<Vec<u8>>>>,
    capacity: usize,
}

#[pymethods]
impl FrameQueue {
    /// Create a new queue with the given maximum number of frames.
    #[new]
    fn new(capacity: usize) -> Self {
        FrameQueue {
            inner: Arc::new(Mutex::new(VecDeque::with_capacity(capacity))),
            capacity,
        }
    }

    /// Push a frame.  Returns True if queued, False if the queue was full
    /// (frame is dropped — better than blocking the capture thread).
    fn push(&self, buf: &[u8]) -> bool {
        let mut q = self.inner.lock().unwrap();
        if q.len() >= self.capacity {
            return false;
        }
        q.push_back(buf.to_vec());
        true
    }

    /// Pop the oldest frame, or return None if the queue is empty.
    fn pop<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        let mut q = self.inner.lock().unwrap();
        q.pop_front().map(|frame| PyBytes::new_bound(py, &frame))
    }

    /// Drain all queued frames.
    fn clear(&self) {
        self.inner.lock().unwrap().clear();
    }

    fn __len__(&self) -> usize {
        self.inner.lock().unwrap().len()
    }

    fn capacity(&self) -> usize {
        self.capacity
    }
}

// ─────────────────────────────────────────────────────────────────────────────
//  Module registration
// ─────────────────────────────────────────────────────────────────────────────

#[pymodule]
fn vhs_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(detect_blue, m)?)?;
    m.add_function(wrap_pyfunction!(bgr_to_rgb, m)?)?;
    m.add_class::<FrameQueue>()?;
    Ok(())
}
