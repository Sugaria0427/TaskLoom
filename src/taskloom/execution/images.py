"""Bounded raster decoding shared by uploads and file-reading tools."""

import io
import warnings

from PIL import Image

Image.MAX_IMAGE_PIXELS = 8_000_000
MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


def inspect_image(data: bytes) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in MIMES or image.width * image.height > 8_000_000:
                    raise ValueError("Unsupported or oversized raster image")
                mime = MIMES[image.format]
                image.verify()
                return mime
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("Invalid or oversized raster image") from None


def model_image(data: bytes) -> bytes:
    inspect_image(data)
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGB")
        image.thumbnail((1280, 1280))
        for _ in range(8):
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=80)
            if output.tell() <= 131072:
                return output.getvalue()
            image.thumbnail((max(1, int(image.width * 0.8)), max(1, int(image.height * 0.8))))
    raise ValueError("Image could not be reduced within the model input limit")
