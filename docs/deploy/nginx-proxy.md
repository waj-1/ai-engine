# Deploy WAJ AI Engine with nginx TLS Proxy

This guide provides a tested configuration for running the WAJ AI Engine behind an nginx reverse proxy with TLS termination.

## Overview

The nginx proxy will:
- Handle HTTPS termination
- Forward WebSocket connections for the voice endpoint
- Set proper X-Forwarded-* headers
- Configure ALLOWED_ORIGINS for CORS
- Route to the auth service with external URLs

## Prerequisites

- nginx installed
- Valid SSL certificate (see Obtaining SSL Certificates below)
- Docker and Docker Compose

## Configuration

### 1. nginx Configuration File

Create the following configuration at `/etc/nginx/nginx.conf` or deploy it alongside your docker-compose.yml:

```nginx
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log notice;
pid /var/run/nginx.pid;

events {
    worker_connections 1024;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    
    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for"';
    
    access_log /var/log/nginx/access.log main;
    
    sendfile on;
    tcp_nopush on;
    keepalive_timeout 65;
    types_hash_max_size 2048;
    
    server {
        listen 80;
        server_name your-domain.com www.your-domain.com;
        return 301 https://$host$request_uri;
    }
    
    server {
        listen 443 ssl http2;
        server_name your-domain.com www.your-domain.com;
        
        ssl_certificate /path/to/your/fullchain.pem;
        ssl_certificate_key /path/to/your/privkey.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_ciphers HIGH:!aNULL:!MD5;
        
        # Security headers
        add_header X-Frame-Options DENY;
        add_header X-Content-Type-Options nosniff;
        add_header X-XSS-Protection "1; mode=block";
        
        # Proxy configuration
        location / {
            proxy_pass http://localhost:8000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header X-Forwarded-Host $host;
            proxy_set_header X-Forwarded-Port $server_port;
            
            # CORS headers
            add_header Access-Control-Allow-Origin "https://your-domain.com";
            add_header Access-Control-Allow-Methods "GET, POST, PUT, DELETE, OPTIONS";
            add_header Access-Control-Allow-Headers "*";
            
            # WebSocket support
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
        }
        
        # Voice endpoint WebSocket
        location /v1/agents/ {
            proxy_pass http://localhost:8000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header X-Forwarded-Host $host;
            proxy_set_header X-Forwarded-Port $server_port;
            
            # WebSocket support for voice
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_read_timeout 86400s;
        }
        
        # Auth service endpoint
        location /auth/v1/ {
            proxy_pass http://localhost:9999;  # Adjust port if needed
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header X-Forwarded-Host $host;
            proxy_set_header X-Forwarded-Port $server_port;
        }
    }
}
```

### 2. docker-compose.yml Modifications

Update your `docker-compose.yml` to:

1. Set external URLs for the auth service
2. Configure ALLOWED_ORIGINS

```yaml
version: '3.8'

services:
  # ... other services remain the same ...
  
  engine:
    environment:
      # ... other environment variables ...
      ALLOWED_ORIGINS: "https://your-domain.com"
      EXTERNAL_AUTH_URL: "https://your-domain.com/auth/v1/"
      # ... other environment variables ...
    
  auth:
    environment:
      # ... other environment variables ...
      EXTERNAL_URL: "https://your-domain.com/auth/v1/"
      # ... other environment variables ...
    
  # ... other services remain the same ...
```

### 3. Environment Variables

Set these environment variables in your `.env` file:

```bash
# External URLs
EXTERNAL_AUTH_URL="https://your-domain.com/auth/v1/"
EXTERNAL_GATEWAY_URL="https://your-domain.com"

# CORS settings
ALLOWED_ORIGINS="https://your-domain.com"

# Engine configuration
ENGINE_PORT=8000
GATEWAY_PORT=54421
```

## Obtaining SSL Certificates

### Option 1: Let's Encrypt (Recommended)

Use Certbot to obtain free SSL certificates:

```bash
# Install Certbot
sudo apt install certbot python3-certbot-nginx

# Obtain certificate
sudo certbot --nginx -d your-domain.com -d www.your-domain.com
```

Certbot will automatically configure nginx for HTTPS.

### Option 2: Self-Signed (For Testing)

For testing purposes, you can create a self-signed certificate:

```bash
# Create certificate directory
sudo mkdir -p /etc/nginx/ssl
sudo chown -R $USER:$USER /etc/nginx/ssl

# Generate private key and certificate
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /etc/nginx/ssl/privkey.pem \
  -out /etc/nginx/ssl/fullchain.pem \
  -subj "/C=US/ST=State/L=City/O=Organization/CN=your-domain.com"
```

## Deployment Steps

1. **Update docker-compose.yml** with the modifications above
2. **Update environment variables** in your `.env` file
3. **Configure nginx** with the provided configuration
4. **Obtain SSL certificates** using Let's Encrypt or create self-signed ones
5. **Start the services**:

```bash
# Start nginx
sudo systemctl start nginx
sudo systemctl enable nginx

# Start the engine
docker compose up -d
```

## Testing the Setup

1. **Access the portal**: Visit `https://your-domain.com`
2. **Log in**: Use your admin credentials
3. **Test chat**: Chat with the demo agent
4. **Test voice**: Open a voice call through the proxy

### Verification Checklist

- [ ] Portal loads at `https://your-domain.com`
- [ ] Login works
- [ ] Chat with demo agent works
- [ ] Voice call connects through WebSocket
- [ ] HTTPS is enforced (HTTP redirects to HTTPS)
- [ ] Security headers are present
- [ ] CORS allows your domain

## Troubleshooting

### Common Issues

1. **WebSocket connection fails**: Check nginx WebSocket configuration
2. **CORS errors**: Verify ALLOWED_ORIGINS matches your domain
3. **SSL issues**: Check certificate paths and permissions
4. **502 Bad Gateway**: Verify backend services are running

### Useful Commands

```bash
# Check nginx configuration
sudo nginx -t

# Reload nginx
sudo systemctl reload nginx

# View nginx logs
sudo tail -f /var/log/nginx/error.log

# Check engine logs
docker compose logs engine
```

## References

- [nginx Documentation](https://nginx.org/en/docs/)
- [Let's Encrypt Documentation](https://letsencrypt.org/docs/)
- [WebSocket Proxying with nginx](https://nginx.org/en/docs/http/websocket.html)
