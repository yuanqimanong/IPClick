class IPClickError(Exception):
    pass


class ConfigError(IPClickError):
    pass


class AdapterError(IPClickError):
    pass


class TransportError(IPClickError):
    pass


class ClientClosedError(IPClickError):
    pass


class AuthenticationError(IPClickError):
    pass


class RequestError(IPClickError):
    pass


class ValidationError(IPClickError, ValueError):
    pass


class URLNotAllowedError(ValidationError):
    pass


__all__ = [
    "AdapterError",
    "AuthenticationError",
    "ClientClosedError",
    "ConfigError",
    "IPClickError",
    "RequestError",
    "TransportError",
    "URLNotAllowedError",
    "ValidationError",
]
