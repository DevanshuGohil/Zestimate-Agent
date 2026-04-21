# syntax=docker/dockerfile:1.7
# --------------------------------------------------------------------------
# Build stage: compile TypeScript + bundle with Vite
# --------------------------------------------------------------------------
FROM node:20-alpine AS builder

WORKDIR /app

# Install deps first (separate layer — cached unless package files change)
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts

# Copy source and build
COPY . .
RUN npm run build

# --------------------------------------------------------------------------
# Runtime stage: nginx serves the static bundle + proxies /api to backend
# --------------------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

# Replace default nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy the Vite output
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost/index.html > /dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
