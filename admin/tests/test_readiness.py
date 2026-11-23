"""Test the /ready endpoint for database health checks."""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch
from admin.router import app

client = TestClient(app)


def test_readiness_endpoint_healthy():
    """Test that /ready returns 200 when database is healthy."""
    with patch("admin.router.get_db_connection") as mock_db:
        # Mock a successful database connection
        mock_conn = mock_db.return_value.__enter__.return_value
        mock_cursor = mock_conn.cursor.return_value
        mock_cursor.fetchone.return_value = [1]
        
        response = client.get("/admin/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready", "database": "healthy"}


def test_readiness_endpoint_unhealthy():
    """Test that /ready returns 503 when database is not available."""
    with patch("admin.router.get_db_connection") as mock_db:
        # Mock a database connection failure
        mock_db.side_effect = Exception("Connection failed")
        
        response = client.get("/admin/ready")
        assert response.status_code == 503
        assert "Database not available" in response.json()["detail"]


def test_readiness_endpoint_query_timeout():
    """Test that /ready returns 503 when database query takes too long."""
    with patch("admin.router.get_db_connection") as mock_db:
        # Mock a slow database connection
        mock_conn = mock_db.return_value.__enter__.return_value
        mock_cursor = mock_conn.cursor.return_value
        
        # Simulate a slow query by adding a delay
        original_execute = mock_cursor.execute
        
        def slow_execute(*args, **kwargs):
            import time
            time.sleep(1.5)  # Simulate a slow query
            return original_execute(*args, **kwargs)
            
        mock_cursor.execute = slow_execute
        mock_cursor.fetchone.return_value = [1]
        
        response = client.get("/admin/ready")
        assert response.status_code == 503
        assert "Database query timeout" in response.json()["detail"]


def test_readiness_endpoint_wrong_result():
    """Test that /ready returns 503 when database query returns unexpected result."""
    with patch("admin.router.get_db_connection") as mock_db:
        # Mock a database connection that returns unexpected result
        mock_conn = mock_db.return_value.__enter__.return_value
        mock_cursor = mock_conn.cursor.return_value
        mock_cursor.fetchone.return_value = [0]  # Should be [1]
        
        response = client.get("/admin/ready")
        assert response.status_code == 503
        assert "Database health check failed" in response.json()["detail"]