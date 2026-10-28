# Build the admin UI, then serve it from nginx with /api proxied to the API.
#
# The bundle is built with VITE_API_BASE_URL="" so every request goes to the
# same origin the page was served from and nginx forwards /api to the API
# container. That keeps the browser off a second origin entirely: no CORS
# preflight, no hard-coded localhost:8000 baked into the bundle, and the JWT
# never travels cross-origin.
FROM node:26-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
# npm ci (not install): build the exact locked tree, so an image rebuild can
# never silently pick up a different dependency version than the one tested.
RUN npm ci

COPY index.html vite.config.js ./
COPY src/ src/

ENV VITE_API_BASE_URL=""
RUN npm run build


# Unprivileged nginx: the master process is not root and the image is built to
# run as UID 101. Nothing here needs to bind a privileged port.
FROM nginxinc/nginx-unprivileged:stable-alpine AS runtime

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1

# The base image already runs nginx in the foreground as a non-root user with
# the right signal handling.
