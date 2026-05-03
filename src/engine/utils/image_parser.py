"""Image normalization helpers for vision model inputs."""
import base64
from io import BytesIO

from PIL import Image, ImageOps

MAX_IMAGE_SIDE = 2048
JPEG_QUALITY = 85
JPEG_CONTENT_TYPE = "image/jpeg"
GIF_CONTENT_TYPE = "image/gif"
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def normalize_image_to_jpeg(data: bytes) -> bytes:
    """Normalize image bytes to bounded JPEG bytes."""
    if not data:
        raise ValueError("Image file is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image file is too big.")

    with Image.open(BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGBA")
            background = Image.new("RGBA", image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, image).convert("RGB")
        else:
            image = image.convert("RGB")

        out = BytesIO()
        image.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return out.getvalue()


def bytes_to_data_url(data: bytes, content_type: str) -> str:
    """Format bytes as a base64 data URL."""
    if not data:
        raise ValueError("Image file is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image file is too big.")
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{b64}"


def image_bytes_to_data_url(data: bytes, file_type: str = "") -> str:
    """Format image bytes as a data URL.

    GIFs are preserved as GIF data URLs for video_url payloads. Other image
    formats are normalized to bounded JPEG.
    """
    if file_type.lower() == "gif":
        return bytes_to_data_url(data, GIF_CONTENT_TYPE)
    jpeg = normalize_image_to_jpeg(data)
    return bytes_to_data_url(jpeg, JPEG_CONTENT_TYPE)
