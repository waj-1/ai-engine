# Kubernetes Deployment

This guide provides step-by-step instructions for deploying the AI Engine on Kubernetes with PostgreSQL (including pgvector), REST and auth services, proper secrets management, and health probes.

## Prerequisites

- Kubernetes cluster (v1.21+)
- `kubectl` configured to access your cluster
- Docker (for building images if needed)
- `helm` (optional, for using the provided charts)

## Architecture

The deployment consists of:

1. **PostgreSQL with pgvector**: Database service
2. **REST service**: API layer for database operations
3. **Auth service**: Authentication and authorization
4. **AI Engine**: Main application service
5. **Ingress**: For external access (optional)

## Step 1: Deploy PostgreSQL with pgvector

Create a Kubernetes manifest for PostgreSQL with pgvector:

```yaml
# k8s/postgres.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: postgres-config
  labels:
    app: postgres
data:
  POSTGRES_DB: ai_supabase
  POSTGRES_USER: postgres
  POSTGRES_PASSWORD: postgres
---
apiVersion: v1
kind: Secret
metadata:
  name: postgres-secret
  labels:
    app: postgres
type: Opaque
stringData:
  postgres-password: postgres
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: postgres
  labels:
    app: postgres
spec:
  replicas: 1
  selector:
    matchLabels:
      app: postgres
  template:
    metadata:
      labels:
        app: postgres
    spec:
      containers:
      - name: postgres
        image: supabase/postgres:15.1.1.44
        ports:
        - containerPort: 5432
        envFrom:
        - configMapRef:
            name: postgres-config
        - secretRef:
            name: postgres-secret
        volumeMounts:
        - name: postgres-storage
          mountPath: /var/lib/postgresql/data
        resources:
          requests:
            memory: "1Gi"
            cpu: "500m"
          limits:
            memory: "2Gi"
            cpu: "1000m"
        livenessProbe:
          exec:
            command:
            - pg_isready
            - -U
            - postgres
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          exec:
            command:
            - pg_isready
            - -U
            - postgres
          initialDelaySeconds: 5
          periodSeconds: 5
      volumes:
      - name: postgres-storage
        persistentVolumeClaim:
          claimName: postgres-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: postgres-service
  labels:
    app: postgres
spec:
  selector:
    app: postgres
  ports:
    - protocol: TCP
      port: 5432
      targetPort: 5432
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: postgres-pvc
  labels:
    app: postgres
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
```

Apply the PostgreSQL manifest:

```bash
kubectl apply -f k8s/postgres.yaml
```

## Step 2: Create Application Secrets

Create a Kubernetes secret for the AI Engine configuration:

```bash
kubectl create secret generic ai-engine-secrets \
  --from-literal=SECRET_KEY=your-secret-key-here \
  --from-literal=DATABASE_URL=postgresql://postgres:postgres@postgres-service:5432/ai_supabase
```

Note: Replace `your-secret-key-here` with a strong, randomly generated secret.

## Step 3: Deploy REST and Auth Services

Create manifests for the REST and auth services:

```yaml
# k8s/rest-auth.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: rest-service
  labels:
    app: rest
spec:
  replicas: 1
  selector:
    matchLabels:
      app: rest
  template:
    metadata:
      labels:
        app: rest
    spec:
      containers:
      - name: rest
        image: ghcr.io/supabase/postgrest:v12.2.0
        ports:
        - containerPort: 3000
        env:
        - name: PGRST_DB_URI
          value: "postgresql://postgres:postgres@postgres-service:5432/ai_supabase"
        - name: PGRST_DB_ANON_ROLE
          value: "anon"
        - name: PGRST_JWT_SECRET
          valueFrom:
            secretKeyRef:
              name: ai-engine-secrets
              key: SECRET_KEY
        livenessProbe:
          httpGet:
            path: /
            port: 3000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /
            port: 3000
          initialDelaySeconds: 5
          periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: rest-service
  labels:
    app: rest
spec:
  selector:
    app: rest
  ports:
    - protocol: TCP
      port: 3000
      targetPort: 3000
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: auth-service
  labels:
    app: auth
spec:
  replicas: 1
  selector:
    matchLabels:
      app: auth
  template:
    metadata:
      labels:
        app: auth
    spec:
      containers:
      - name: auth
        image: ghcr.io/supabase/gotrue:v2.138.0
        ports:
        - containerPort: 9999
        env:
        - name: GOTRUE_API_HOST
          value: "0.0.0.0"
        - name: GOTRUE_API_PORT
          value: "9999"
        - name: GOTRUE_DB_DRIVER
          value: "postgres"
        - name: GOTRUE_DB_DATABASE_URL
          value: "postgresql://postgres:postgres@postgres-service:5432/ai_supabase"
        - name: GOTRUE_JWT_ADMIN_ROLES
          value: "service_role"
        - name: GOTRUE_JWT_AUD
          value: "ai-engine"
        - name: GOTRUE_SITE_URL
          value: "http://localhost:8000"
        - name: GOTRUE_JWT_SECRET
          valueFrom:
            secretKeyRef:
              name: ai-engine-secrets
              key: SECRET_KEY
        livenessProbe:
          httpGet:
            path: /=ping
            port: 9999
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /=ping
            port: 9999
          initialDelaySeconds: 5
          periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: auth-service
  labels:
    app: auth
spec:
  selector:
    app: auth
  ports:
    - protocol: TCP
      port: 9999
      targetPort: 9999
```

Apply the REST and auth manifests:

```bash
kubectl apply -f k8s/rest-auth.yaml
```

## Step 4: Deploy the AI Engine

Create a manifest for the main AI Engine application:

```yaml
# k8s/engine.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ai-engine
  labels:
    app: ai-engine
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ai-engine
  template:
    metadata:
      labels:
        app: ai-engine
    spec:
      containers:
      - name: ai-engine
        image: ghcr.io/waj-1/ai-engine:latest
        ports:
        - containerPort: 8000
        env:
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: ai-engine-secrets
              key: DATABASE_URL
        - name: SECRET_KEY
          valueFrom:
            secretKeyRef:
              name: ai-engine-secrets
              key: SECRET_KEY
        - name: SUPABASE_URL
          value: "http://rest-service:3000"
        - name: SUPABASE_ANON_KEY
          value: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub24iLCJleHAiOjE5ODM4MTI5OTZ9.CRXP1A7WOeoJeXxjNni43kdQwgnWNReilDMblYTn_I0"
        - name: SUPABASE_SERVICE_ROLE_KEY
          value: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0.EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
        - name: PORTAL_NAME
          value: "AI Engine Portal"
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 5
        resources:
          requests:
            memory: "1Gi"
            cpu: "500m"
          limits:
            memory: "2Gi"
            cpu: "1000m"
      restartPolicy: Always
---
apiVersion: v1
kind: Service
metadata:
  name: ai-engine-service
  labels:
    app: ai-engine
spec:
  selector:
    app: ai-engine
  ports:
    - protocol: TCP
      port: 80
      targetPort: 8000
  type: LoadBalancer
```

Apply the AI Engine manifest:

```bash
kubectl apply -f k8s/engine.yaml
```

## Step 5: Set up Ingress (Optional)

For external access, create an Ingress resource:

```yaml
# k8s/ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ai-engine-ingress
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /
spec:
  rules:
  - host: ai-engine.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: ai-engine-service
            port:
              number: 80
```

Apply the Ingress:

```bash
kubectl apply -f k8s/ingress.yaml
```

## Step 6: Verify the Deployment

1. Check the status of all deployments:

```bash
kubectl get pods -l app=postgres,app=rest,app=auth,app=ai-engine
```

2. Check the services:

```bash
kubectl get svc
```

3. Forward the AI Engine service to your local machine:

```bash
kubectl port-forward svc/ai-engine-service 8000:80
```

4. Access the application at `http://localhost:8000`

5. Verify the first-run page appears and the demo agent answers

## Troubleshooting

- Check pod logs: `kubectl logs <pod-name>`
- Describe pods for details: `kubectl describe pod <pod-name>`
- Check service status: `kubectl get svc <service-name>`

## Next Steps

1. Set up monitoring and logging
2. Configure horizontal pod autoscaling
3. Set up backup and disaster recovery
4. Configure SSL/TLS for production

## References

- [PostgreSQL on Kubernetes](https://kubernetes.io/docs/tasks/run-application/run-stateless-application-deployment/)
- [Kubernetes Secrets](https://kubernetes.io/docs/concepts/configuration/secret/)
- [Kubernetes Health Checks](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-probes/)