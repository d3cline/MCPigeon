import hmac
import hashlib
import logging
from typing import Optional, Tuple

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from rest_framework import authentication, exceptions
from rest_framework.request import Request

logger = logging.getLogger("mcp.auth")

# Configuration constants
PAT_ALGORITHM = 'sha256'
# No prefix for this app. Keep empty to emit bare tokens.
PAT_PREFIX = ''
PAT_HEADER_PREFIX = 'Bearer'


class PATAuthentication(authentication.BaseAuthentication):
    """
    Custom authentication for MVA trucking dispatch system.
    
    Uses deterministic Personal Access Tokens based on:
    - Username (unique identifier)
    - User creation timestamp (ensures uniqueness across recreated users)
    - User's password hash (causes token to change on password reset)
    
    This approach eliminates the need for token database storage while
    providing secure authentication suitable for trucking industry hardware.
    """
    
    keyword = PAT_HEADER_PREFIX
    model = User
    
    def authenticate(self, request: Request) -> Optional[Tuple[User, str]]:
        """
        Authenticate the request using PAT token.
        
        Returns:
            Tuple of (user, token) if authentication succeeds
            None if no authentication attempted
            
        Raises:
            AuthenticationFailed if authentication attempted but failed
        """
        auth_header = self.get_authorization_header(request)
        if not auth_header:
            return None
            
        try:
            token = self.get_token_from_header(auth_header)
            if not token:
                return None
                
            user = self.authenticate_token(token)
            if not user:
                raise exceptions.AuthenticationFailed('Invalid token')
                
            return (user, token)
            
        except (UnicodeError, ValueError) as e:
            logger.warning(f"Authentication header parsing failed: {e}")
            raise exceptions.AuthenticationFailed('Invalid token header')
    
    def authenticate_header(self, request: Request) -> str:
        """
        Return a string to be used as the value of the `WWW-Authenticate`
        header in a `401 Unauthenticated` response.
        """
        return f'{self.keyword} realm="api"'
    
    def get_authorization_header(self, request: Request) -> bytes:
        """
        Extract the authorization header from the request.
        
        Returns the header value as bytes, or empty bytes if not found.
        """
        auth = request.META.get('HTTP_AUTHORIZATION', '')
        if isinstance(auth, str):
            auth = auth.encode('iso-8859-1')
        return auth
    
    def get_token_from_header(self, auth_header: bytes) -> Optional[str]:
        """
        Extract the token from the authorization header.
        
        Expected format: "Bearer <token>" or just "<token>"
        """
        auth_header = auth_header.decode('iso-8859-1').strip()
        
        if not auth_header:
            return None
            
        # Handle "Bearer <token>" format
        if auth_header.lower().startswith(f'{self.keyword.lower()} '):
            token = auth_header[len(self.keyword):].strip()
        else:
            # Handle raw token format for simple clients
            token = auth_header
            
        # Remove optional prefix if present (none for this app)
        if PAT_PREFIX and token.startswith(PAT_PREFIX):
            token = token[len(PAT_PREFIX):]
            
        return token if token else None
    
    def authenticate_token(self, provided_token: str) -> Optional[User]:
        """
        Authenticate a token by checking it against all users.
        
        This iterates through users to find matching token, which is acceptable
        for moderate user counts typical in trucking operations.
        
        For large user bases, consider adding a token-to-username mapping cache.
        """
        # Basic token validation
        if not provided_token or len(provided_token) < 32:
            logger.debug("Token too short or empty")
            return None
        
        # Try to find user with matching token
        # Note: For very large user bases, implement username extraction 
        # or caching to avoid O(n) lookup
        for user in User.objects.filter(is_active=True).iterator():
            expected_token = self.generate_user_token(user)
            
            # Use constant-time comparison to prevent timing attacks
            if self.constant_time_compare(provided_token, expected_token):
                logger.info(f"Successful PAT authentication for user: {user.username}")
                return user
        
        logger.warning(f"PAT authentication failed - no matching user found")
        return None
    
    def generate_user_token(self, user: User) -> str:
        """
        Generate the expected PAT for a given user.

        Token = HMAC-SHA256(
            key = user.password,  # Django's hashed password string
            msg = username + user.date_joined.isoformat()
        )

        Using date_joined preserves uniqueness across account recreation,
        and using the password hash ensures tokens rotate on password reset.
        """
        if not user.date_joined:
            raise ValidationError(f"User {user.username} has no creation date")
        if not user.password:
            raise ValidationError(f"User {user.username} has no password set")
        
        # Construct the message to sign
        # Format: username + ISO timestamp
        message = user.username + user.date_joined.isoformat()

        # Generate HMAC token using the user's password hash as key
        token = hmac.new(
            key=user.password.encode('utf-8'),
            msg=message.encode('utf-8'),
            digestmod=getattr(hashlib, PAT_ALGORITHM)
        ).hexdigest()
        
        return token
    
    def constant_time_compare(self, val1: str, val2: str) -> bool:
        """
        Compare two strings in constant time to prevent timing attacks.
        
        Uses Django's built-in implementation for security.
        """
        from django.utils.crypto import constant_time_compare
        return constant_time_compare(val1, val2)


# Utility functions for token management
def generate_pat_for_user(user: User) -> str:
    """
    Generate a PAT for a specific user.
    
    This is the primary function for generating tokens that drivers/devices
    will use for authentication. Store this token securely on the device.
    
    Args:
        user: Django User instance
        
    Returns:
        PAT token string with optional prefix
        
    Example:
        pat = generate_pat_for_user(driver_user)
        # Return: "a1b2c3d4e5f6..." (no prefix)
    """
    auth_backend = PATAuthentication()
    raw_token = auth_backend.generate_user_token(user)
    
    # No prefix for this app
    return raw_token


def validate_pat_token(token: str) -> Optional[User]:
    """
    Validate a PAT token and return the associated user.
    
    Useful for testing or non-DRF authentication scenarios.
    
    Args:
        token: PAT token string (with or without prefix)
        
    Returns:
        User instance if valid, None if invalid
        
    Example:
        user = validate_pat_token("mva_a1b2c3d4e5f6...")
        if user:
            print(f"Valid token for {user.username}")
    """
    auth_backend = PATAuthentication()
    return auth_backend.authenticate_token(token)


def regenerate_user_token(username: str) -> Optional[str]:
    """
    Regenerate a token for a user.

    Preferred approach with password-hash-based tokens: reset the user's
    password. The PAT changes automatically when `user.password` changes.

    This legacy helper performs account recreation to force date_joined to
    change as well. Avoid this unless you explicitly want a new date_joined.
    
    Args:
        username: Username to regenerate token for
        
    Returns:
        New PAT token if successful, None if user not found
        
    Example:
        new_token = regenerate_user_token("driver01")
        # User account recreated with new creation time = new token
    """
    try:
        # Get existing user data
        old_user = User.objects.get(username=username)
        user_data = {
            'username': old_user.username,
            'email': old_user.email,
            'first_name': old_user.first_name,
            'last_name': old_user.last_name,
            'is_active': old_user.is_active,
            'is_staff': old_user.is_staff,
            'is_superuser': old_user.is_superuser,
        }
        groups = list(old_user.groups.all())
        
        # Delete old user (this changes the creation timestamp)
        old_user.delete()
        
        # Create new user with same data but new timestamp
        new_user = User.objects.create_user(**user_data)
        new_user.groups.set(groups)
        
        # Generate new token
        new_token = generate_pat_for_user(new_user)
        
        logger.info(f"Token regenerated for user: {username}")
        return new_token
        
    except User.DoesNotExist:
        logger.error(f"Cannot regenerate token - user not found: {username}")
        return None
    except Exception as e:
        logger.error(f"Token regeneration failed for {username}: {e}")
        return None


# Management command helpers
def create_driver_with_pat(username: str, **user_kwargs) -> Tuple[User, str]:
    """
    Create a new driver user and return their PAT.
    
    Convenience function for setting up new driver accounts with tokens.
    
    Args:
        username: Unique username for the driver
        **user_kwargs: Additional User model fields (email, first_name, etc.)
        
    Returns:
        Tuple of (User instance, PAT token)
        
    Example:
        user, token = create_driver_with_pat(
            "driver01", 
            first_name="John", 
            last_name="Smith",
            email="john@trucking.com"
        )
        print(f"Driver {user.username} created with token: {token}")
    """
    # Create the user
    user = User.objects.create_user(username=username, **user_kwargs)
    
    # Generate their PAT
    token = generate_pat_for_user(user)
    
    logger.info(f"Created driver {username} with PAT")
    return user, token


# Security settings and recommendations
"""
Security Configuration Recommendations:

1. HTTPS Only:
   - Ensure your API is only accessible over HTTPS in production
   - Tokens transmitted in clear text over HTTP can be intercepted

2. Token Storage:
   - Store tokens securely on driver devices (encrypted preferences/keychain)
   - Never log tokens in plain text
   - Educate users to keep devices secure

3. Rate Limiting:
   - Implement rate limiting on authentication endpoints
   - Consider IP-based blocking for repeated failures
   
4. Monitoring:
   - Log authentication attempts and failures
   - Monitor for unusual token usage patterns
   - Alert on token validation failures

5. Secret Key Management:
   - Keep Django SECRET_KEY secure and unique per environment
   - Consider using MVA_PAT_SECRET for additional token key isolation
   - Rotate SECRET_KEY if compromised (will invalidate all tokens)

6. Token Lifecycle:
   - Document token regeneration process for support staff
   - Establish procedures for compromised device handling
   - Consider implementing optional token expiration for high-security scenarios

Example settings.py configuration:

    # Custom secret for PAT generation (optional)
    MVA_PAT_SECRET = "your-additional-secret-key-here"
    
    # DRF Authentication settings
    REST_FRAMEWORK = {
        'DEFAULT_AUTHENTICATION_CLASSES': [
            'dispatch.auth.TruckingPATAuthentication',
            'rest_framework.authentication.SessionAuthentication',  # For web UI
        ],
        'DEFAULT_PERMISSION_CLASSES': [
            'rest_framework.permissions.IsAuthenticated',
        ],
    }
    
    # Rate limiting (if using django-ratelimit)
    RATELIMIT_ENABLE = True
    
    # Logging configuration
    LOGGING = {
        'loggers': {
            'mcp.auth': {
                'handlers': ['file'],
                'level': 'INFO',
                'propagate': False,
            },
        },
    }
"""
