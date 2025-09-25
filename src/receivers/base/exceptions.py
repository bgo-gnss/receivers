"""Exception classes for receivers package."""


class ReceiverError(Exception):
    """Base exception for receiver-related errors."""

    def __init__(self, message: str = "", *args, **kwargs):
        """Initialize receiver error with message and optional context."""
        # Call parent with only the message and positional args
        super().__init__(message, *args)
        self.message = message
        # Store additional context as attributes
        for key, value in kwargs.items():
            setattr(self, key, value)


class ConnectionError(ReceiverError):
    """Exception raised when connection to receiver fails."""

    pass


class DownloadError(ReceiverError):
    """Exception raised when data download fails."""

    pass


class ConfigurationError(ReceiverError):
    """Exception raised when receiver configuration is invalid."""

    pass


class HealthCheckError(ReceiverError):
    """Exception raised when receiver health check fails."""

    pass


class AuthenticationError(ReceiverError):
    """Exception raised when authentication fails."""

    pass
