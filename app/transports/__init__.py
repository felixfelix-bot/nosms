from .base import Capabilities, SendResult, Transport
from .email_gateway import EmailGatewayTransport, UnsupportedDestination
from .fake import FakeTransport

__all__ = ["Capabilities", "SendResult", "Transport",
           "EmailGatewayTransport", "UnsupportedDestination", "FakeTransport"]
