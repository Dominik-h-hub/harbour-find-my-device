from qrcode.main import QRCode
from qrcode.main import make  # noqa
from qrcode.constants import (  # noqa
    ERROR_CORRECT_L,
    ERROR_CORRECT_M,
    ERROR_CORRECT_Q,
    ERROR_CORRECT_H,
)

# Vendored trim: the image subpackage (renderers / pypng / Pillow) is not
# bundled. This app only consumes QRCode.get_matrix(); see main.py.
