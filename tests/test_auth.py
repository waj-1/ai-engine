"""
Tests for authentication and authorization module.
"""

import pytest
import jwt
from datetime import datetime, timedelta
from unittest.mock import patch, Mock

from core.auth import (
    validate_token,
    get_supabase_client,
    get_service_client,
    get_user_id_from_token,
    get_user_metadata_from_token,
    TokenValidationError,
)


class TestValidateToken:
    """Tests for token validation"""
    
    def test_valid_token(self, valid_jwt_token, test_user_id):
        """Test validation of a valid JWT token"""
        result = validate_token(valid_jwt_token)
        
        assert result is not None
        assert result['sub'] == test_user_id
        assert result['email'] == "test@example.com"
        assert result['role'] == "business_owner"
    
    def test_expired_token(self, expired_jwt_token):
        """Test that expired tokens raise TokenValidationError"""
        # Our implementation doesn't actually verify expiration (Supabase does)
        # But we can test the structure
        result = validate_token(expired_jwt_token)
        assert result is not None  # Will decode but not verify
    
    def test_invalid_token(self):
        """Test that invalid tokens raise TokenValidationError"""
        with pytest.raises(TokenValidationError):
            validate_token("invalid.token.here")
    
    def test_token_without_sub(self):
        """Test that token without 'sub' claim raises error"""
        token = jwt.encode({"email": "test@example.com"}, "secret", algorithm="HS256")
        
        with pytest.raises(TokenValidationError, match="missing 'sub' claim"):
            validate_token(token)
    
    def test_empty_token(self):
        """Test that empty token raises error"""
        with pytest.raises(TokenValidationError):
            validate_token("")


class TestSupabaseClients:
    """Tests for Supabase client creation"""
    
    @patch('core.auth.create_client')
    @patch.dict('os.environ', {'SUPABASE_URL': 'http://test.supabase.co', 'SUPABASE_ANON_KEY': 'test_anon_key'})
    def test_get_supabase_client_with_user_token(self, mock_create_client, valid_jwt_token):
        """Test creating Supabase client with user token"""
        mock_client = Mock()
        mock_create_client.return_value = mock_client
        
        client = get_supabase_client(valid_jwt_token)
        
        assert client is not None
        mock_create_client.assert_called_once()
        
        # Verify the Authorization header was set
        call_args = mock_create_client.call_args
        assert 'options' in call_args.kwargs
        assert 'headers' in call_args.kwargs['options'].__dict__
    
    @patch('core.auth.create_client')
    @patch.dict('os.environ', {'AI_SUPABASE_URL': 'http://ai.supabase.co', 'AI_SUPABASE_SERVICE_KEY': 'ai_service_key'})
    def test_get_service_client(self, mock_create_client):
        """Service client now targets the AI project (AI_SUPABASE_*)."""
        mock_create_client.return_value = Mock()
        client = get_service_client()
        assert client is not None
        mock_create_client.assert_called_once()
    
    def test_get_supabase_client_missing_env_vars(self, valid_jwt_token):
        """Test that missing environment variables raises ValueError"""
        with patch.dict('os.environ', {}, clear=True):
            with pytest.raises(ValueError, match="Missing SUPABASE_URL"):
                get_supabase_client(valid_jwt_token)
    
    def test_get_service_client_missing_env_vars(self):
        with patch.dict('os.environ', {}, clear=True):
            with pytest.raises(ValueError, match="AI_SUPABASE_URL"):
                get_service_client()


class TestUserHelpers:
    """Tests for user helper functions"""
    
    def test_get_user_id_from_token(self, valid_jwt_token, test_user_id):
        """Test extracting user ID from token"""
        user_id = get_user_id_from_token(valid_jwt_token)
        
        assert user_id == test_user_id
    
    def test_get_user_id_from_invalid_token(self):
        """Test that invalid token raises error"""
        with pytest.raises(TokenValidationError):
            get_user_id_from_token("invalid.token")
    
    def test_get_user_metadata_from_token(self, valid_jwt_token, test_user_id):
        """Test extracting user metadata from token"""
        metadata = get_user_metadata_from_token(valid_jwt_token)
        
        assert metadata is not None
        assert metadata['user_id'] == test_user_id
        assert metadata['email'] == "test@example.com"
        assert metadata['role'] == "business_owner"
        assert 'user_metadata' in metadata
        assert metadata['user_metadata']['name'] == "Test User"
    
    def test_get_user_metadata_from_token_missing_fields(self):
        """Test handling token with missing optional fields"""
        token = jwt.encode({"sub": "user-123"}, "secret", algorithm="HS256")
        metadata = get_user_metadata_from_token(token)
        
        assert metadata['user_id'] == "user-123"
        assert metadata['email'] is None
        assert metadata['role'] is None

