# ==============================================================================
# Multi-stage Dockerfile for Shorekeeper S2S Client (Svelte 5 + Nginx Slim)
# ==============================================================================

FROM node:22-alpine AS builder

WORKDIR /build

# Enable pnpm with pinned stable version (9.15.9 matching lockfile)
RUN npm install -g pnpm@9.15.9

# Copy root monorepo package manifests
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/client/package.json ./apps/client/
COPY packages/contracts/package.json ./packages/contracts/
COPY packages/event-bus/package.json ./packages/event-bus/
COPY packages/conflict-map/package.json ./packages/conflict-map/
COPY packages/task-store/package.json ./packages/task-store/
COPY packages/merge-orchestrator/package.json ./packages/merge-orchestrator/
COPY packages/observability/package.json ./packages/observability/
COPY packages/omp-bridge/package.json ./packages/omp-bridge/

RUN pnpm install --frozen-lockfile

# Copy source files
COPY tsconfig.base.json ./
COPY packages/ ./packages/
COPY apps/client/ ./apps/client/

# Build dependencies and client bundle
RUN pnpm -r build

# ==============================================================================
# Production Runtime Stage (Nginx Alpine <25MB)
# ==============================================================================
FROM nginx:alpine-slim AS runtime

# Copy custom Nginx configuration
COPY <<'EOF' /etc/nginx/conf.d/default.conf
server {
    listen 80;
    server_name _;

    root /usr/share/nginx/html;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location ~* \.(?:css|js|jpg|jpeg|gif|png|ico|cur|gz|svg|svgz|mp4|ogg|ogv|webm|htc|woff|woff2)$ {
        expires 1y;
        access_log off;
        add_header Cache-Control "public, immutable";
    }
}
EOF

# Copy static assets from builder
COPY --from=builder /build/apps/client/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
