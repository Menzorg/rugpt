"""Image normalization helpers for vision model inputs."""
import base64
from io import BytesIO

from PIL import Image, ImageOps

MAX_IMAGE_SIDE = 2048
JPEG_QUALITY = 85
JPEG_CONTENT_TYPE = "image/jpeg"


def normalize_image_to_jpeg(data: bytes) -> bytes:
    """Normalize image bytes to bounded JPEG bytes."""
    if not data:
        raise ValueError("Image file is empty.")

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


def image_bytes_to_data_url(data: bytes) -> str:
    """Normalize image bytes and format as JPEG base64 data URL."""
    jpeg = normalize_image_to_jpeg(data)
    b64 = base64.b64encode(jpeg).decode("ascii")
    return f"data:{JPEG_CONTENT_TYPE};base64,{b64}"
