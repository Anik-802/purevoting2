# Web (D-29): build the voter / admin web, serve it with nginx together with Core's /v1/ API on one origin.
FROM node:24.11.0-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html tsconfig.json vite.config.ts ./
COPY src ./src
RUN npm run build

FROM nginx:1.27.2-alpine
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/purevoting.conf
COPY pv_proxy.conf /etc/nginx/pv_proxy.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 8080
