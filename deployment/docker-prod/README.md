# Production Docker Setup

**Status**: 🚧 Not yet implemented

This directory will contain the production Docker setup for deployment to IMO production servers.

## Planned Features

### Production vs Development Differences

**Development** (`deployment/docker-dev/`):
- ✅ Source code mounted as volumes (live updates)
- ✅ Editable package installs (`pip install -e`)
- ✅ Hot-reload without rebuilds
- ✅ Git branch switching support
- ✅ Development tools included (vim, nano, htop)
- ⚠️ Larger image size
- ⚠️ Requires source code on host

**Production** (this directory, future):
- 📦 Source code baked into image
- 📦 Proper multi-stage builds
- 📦 Minimal final image size
- 🔒 Security hardening (non-root user, minimal packages)
- 🔒 Read-only root filesystem
- 🚀 Optimized for performance
- 🚀 No external dependencies at runtime
- ✅ Version-tagged images
- ✅ Health checks and monitoring

## Future Implementation

When creating the production setup, consider:

1. **Multi-stage Docker build**:
   ```dockerfile
   # Stage 1: Build dependencies
   FROM python:3.11-slim as builder
   COPY requirements.txt .
   RUN pip wheel --no-cache-dir -r requirements.txt

   # Stage 2: Runtime
   FROM python:3.11-slim
   COPY --from=builder /wheels /wheels
   RUN pip install --no-cache /wheels/*
   COPY src/ /app/src/
   ...
   ```

2. **Versioned images**: Tag with git commit/version
   ```bash
   docker build -t gps-receivers:v1.2.3 .
   docker build -t gps-receivers:latest .
   ```

3. **Configuration**:
   - Config files baked into image OR
   - Config from environment variables/secrets
   - No git repository cloning at runtime

4. **Security**:
   - Scan images for vulnerabilities
   - Use official Python slim base images
   - Drop unnecessary capabilities
   - Run as non-root user

5. **CI/CD Integration**:
   - Automated builds on git tag
   - Push to container registry
   - Automated deployment to servers

## Current Recommendation

For now, use `deployment/docker-dev/` for:
- Development and testing
- Server deployments (until production setup ready)

The development setup is **perfectly suitable** for production use with the IMO network,
since the server has access to the source code repository.

## When to Implement Production Setup

Consider implementing when:
- Deploying to external servers without source access
- Need to distribute as standalone images
- Require strict security compliance
- Image size becomes a concern
- CI/CD pipeline is established

## Migration Path

1. Keep `docker-dev/` as primary deployment method
2. Create `docker-prod/` when scaling requirements demand it
3. Both can coexist for different deployment scenarios

---

**See Also**:
- [Development Docker Workflow](../../docs/development/docker-workflow.md)
- [Current Development Setup](../docker-dev/README.md)
