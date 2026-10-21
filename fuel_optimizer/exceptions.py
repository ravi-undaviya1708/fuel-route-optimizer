from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status
import logging

logger = logging.getLogger(__name__)


class FuelOptimizerBaseException(Exception):
    """Base exception for fuel optimizer application."""
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_message = "An unexpected error occurred."

    def __init__(self, message=None, details=None):
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        self.details = details or {}


class LocationNotFoundError(FuelOptimizerBaseException):
    """Raised when an address cannot be found by geocoding service."""
    status_code = status.HTTP_400_BAD_REQUEST
    default_message = "The specified location could not be found."


class NonUSLocationError(FuelOptimizerBaseException):
    """Raised when a geocoded location is outside the United States."""
    status_code = status.HTTP_400_BAD_REQUEST
    default_message = "Only locations within the United States are supported."


class UpstreamServiceError(FuelOptimizerBaseException):
    """Raised when an upstream external service (Nominatim, OSRM) fails."""
    status_code = status.HTTP_502_BAD_GATEWAY
    default_message = "Upstream service request failed."


class UpstreamServiceUnavailableError(FuelOptimizerBaseException):
    """Raised when an upstream service is temporarily unreachable or times out."""
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_message = "Upstream service is currently unavailable or timed out."


def custom_exception_handler(exc, context):
    """
    Custom exception handler to ensure standard, consistent JSON error responses
    across all API endpoints.
    """
    # First, let standard DRF handler process built-in DRF exceptions
    response = exception_handler(exc, context)

    if isinstance(exc, FuelOptimizerBaseException):
        logger.warning("Application exception [%s]: %s (details: %s)",
                       exc.__class__.__name__, exc.message, exc.details)
        return Response(
            {
                "error": exc.message,
                "error_type": exc.__class__.__name__,
                "details": exc.details if exc.details else None,
            },
            status=exc.status_code,
        )

    if response is not None:
        # Standardize DRF validation error / auth error format
        error_msg = "Validation failed"
        if isinstance(response.data, dict) and 'detail' in response.data:
            error_msg = str(response.data['detail'])
        elif isinstance(response.data, dict):
            # Collect first error message if available
            first_key = next(iter(response.data))
            val = response.data[first_key]
            if isinstance(val, list) and val:
                error_msg = f"{first_key}: {val[0]}"
            else:
                error_msg = f"{first_key}: {val}"

        response.data = {
            "error": error_msg,
            "error_type": exc.__class__.__name__,
            "details": response.data,
        }
        return response

    # Unhandled exception
    logger.exception("Unhandled server exception: %s", exc)
    return Response(
        {
            "error": "Internal server error occurred.",
            "error_type": exc.__class__.__name__,
            "details": None,
        },
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
