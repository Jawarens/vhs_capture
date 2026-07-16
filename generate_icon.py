"""Generate a VHS tape icon for the application."""
import struct
import zlib
import os

def create_vhs_icon():
    """Create a multi-size .ico file with a VHS tape design."""
    sizes = [16, 32, 48, 64, 128, 256]
    images = []

    for size in sizes:
        img = draw_vhs_tape(size)
        images.append((size, img))

    write_ico(images, os.path.join(os.path.dirname(__file__), "vhs_capture.ico"))
    print("Created vhs_capture.ico")


def draw_vhs_tape(size):
    """Draw a VHS tape icon at the given size. Returns RGBA pixel data."""
    pixels = [[0, 0, 0, 0]] * (size * size)

    def set_pixel(x, y, r, g, b, a=255):
        if 0 <= x < size and 0 <= y < size:
            pixels[y * size + x] = [r, g, b, a]

    def fill_rect(x1, y1, x2, y2, r, g, b, a=255):
        for y in range(max(0, y1), min(size, y2)):
            for x in range(max(0, x1), min(size, x2)):
                set_pixel(x, y, r, g, b, a)

    def fill_circle(cx, cy, radius, r, g, b, a=255):
        for y in range(max(0, int(cy - radius)), min(size, int(cy + radius) + 1)):
            for x in range(max(0, int(cx - radius)), min(size, int(cx + radius) + 1)):
                if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
                    set_pixel(x, y, r, g, b, a)

    def draw_rounded_rect(x1, y1, x2, y2, r, g, b, a=255, radius=0):
        """Fill a rounded rectangle."""
        for y in range(max(0, y1), min(size, y2)):
            for x in range(max(0, x1), min(size, x2)):
                # Check corners
                in_rect = True
                if x < x1 + radius and y < y1 + radius:
                    in_rect = (x - (x1 + radius))**2 + (y - (y1 + radius))**2 <= radius**2
                elif x >= x2 - radius and y < y1 + radius:
                    in_rect = (x - (x2 - radius - 1))**2 + (y - (y1 + radius))**2 <= radius**2
                elif x < x1 + radius and y >= y2 - radius:
                    in_rect = (x - (x1 + radius))**2 + (y - (y2 - radius - 1))**2 <= radius**2
                elif x >= x2 - radius and y >= y2 - radius:
                    in_rect = (x - (x2 - radius - 1))**2 + (y - (y2 - radius - 1))**2 <= radius**2
                if in_rect:
                    set_pixel(x, y, r, g, b, a)

    # Scale factor
    s = size / 64.0

    # Main cassette body - dark charcoal
    body_x1 = int(4 * s)
    body_y1 = int(12 * s)
    body_x2 = int(60 * s)
    body_y2 = int(52 * s)
    corner_r = int(4 * s)
    draw_rounded_rect(body_x1, body_y1, body_x2, body_y2, 45, 45, 50, radius=corner_r)

    # Top edge highlight
    fill_rect(int(8 * s), int(12 * s), int(56 * s), int(14 * s), 65, 65, 70)

    # Label area - cream/white
    label_x1 = int(10 * s)
    label_y1 = int(16 * s)
    label_x2 = int(54 * s)
    label_y2 = int(34 * s)
    fill_rect(label_x1, label_y1, label_x2, label_y2, 240, 235, 220)

    # Label stripes (VHS branding colors)
    stripe_h = max(1, int(2 * s))
    # Blue stripe
    fill_rect(label_x1, int(18 * s), label_x2, int(18 * s) + stripe_h, 40, 80, 160)
    # Red stripe
    fill_rect(label_x1, int(21 * s), label_x2, int(21 * s) + stripe_h, 180, 40, 40)
    # Gold/yellow stripe
    fill_rect(label_x1, int(24 * s), label_x2, int(24 * s) + stripe_h, 200, 160, 40)

    # "VHS" text approximation (just a darker rectangle for small sizes)
    if size >= 32:
        text_y = int(27 * s)
        text_h = max(2, int(4 * s))
        fill_rect(int(20 * s), text_y, int(44 * s), text_y + text_h, 60, 60, 60)

    # Tape window - dark area showing reels
    window_x1 = int(14 * s)
    window_y1 = int(36 * s)
    window_x2 = int(50 * s)
    window_y2 = int(48 * s)
    fill_rect(window_x1, window_y1, window_x2, window_y2, 20, 20, 25)

    # Left reel
    reel_left_cx = int(24 * s)
    reel_cy = int(42 * s)
    reel_r = int(5 * s)
    fill_circle(reel_left_cx, reel_cy, reel_r, 60, 60, 65)
    fill_circle(reel_left_cx, reel_cy, int(2 * s), 30, 30, 35)

    # Right reel
    reel_right_cx = int(40 * s)
    fill_circle(reel_right_cx, reel_cy, reel_r, 60, 60, 65)
    fill_circle(reel_right_cx, reel_cy, int(2 * s), 30, 30, 35)

    # Tape between reels
    tape_y1 = int(41 * s)
    tape_y2 = int(43 * s)
    fill_rect(int(29 * s), tape_y1, int(35 * s), tape_y2, 80, 50, 30)

    # Bottom flap detail
    fill_rect(int(18 * s), int(48 * s), int(46 * s), int(50 * s), 35, 35, 40)

    # Small red record indicator dot (top-right area)
    dot_cx = int(52 * s)
    dot_cy = int(10 * s)
    dot_r = max(1, int(2.5 * s))
    fill_circle(dot_cx, dot_cy, dot_r, 220, 50, 50)

    return pixels


def write_ico(images, filepath):
    """Write images as a .ico file. Each image is (size, pixel_list)."""
    num_images = len(images)

    # ICO header: 6 bytes
    header = struct.pack('<HHH', 0, 1, num_images)

    # Build PNG data for each image
    png_data_list = []
    for size, pixels in images:
        png_bytes = encode_png(size, size, pixels)
        png_data_list.append(png_bytes)

    # Directory entries: 16 bytes each
    offset = 6 + 16 * num_images
    entries = b''
    for i, (size, pixels) in enumerate(images):
        w = 0 if size >= 256 else size
        h = 0 if size >= 256 else size
        data_size = len(png_data_list[i])
        entries += struct.pack('<BBBBHHII', w, h, 0, 0, 1, 32, data_size, offset)
        offset += data_size

    with open(filepath, 'wb') as f:
        f.write(header)
        f.write(entries)
        for png_bytes in png_data_list:
            f.write(png_bytes)


def encode_png(width, height, pixels):
    """Encode RGBA pixel data as a PNG file."""
    def write_chunk(chunk_type, data):
        chunk = chunk_type + data
        crc = zlib.crc32(chunk) & 0xFFFFFFFF
        return struct.pack('>I', len(data)) + chunk + struct.pack('>I', crc)

    # PNG signature
    sig = b'\x89PNG\r\n\x1a\n'

    # IHDR
    ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    ihdr = write_chunk(b'IHDR', ihdr_data)

    # IDAT
    raw_data = b''
    for y in range(height):
        raw_data += b'\x00'  # filter byte
        for x in range(width):
            p = pixels[y * width + x]
            raw_data += struct.pack('BBBB', p[0], p[1], p[2], p[3])

    compressed = zlib.compress(raw_data)
    idat = write_chunk(b'IDAT', compressed)

    # IEND
    iend = write_chunk(b'IEND', b'')

    return sig + ihdr + idat + iend


if __name__ == '__main__':
    create_vhs_icon()
