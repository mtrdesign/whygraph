# Caddy with the Cloudflare DNS plugin, needed for the wildcard certificate (DNS-01).
# Stock Caddy has no DNS providers; swap the plugin for your DNS host's if it is not Cloudflare.
FROM caddy:2.10-builder AS build
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2.10
COPY --from=build /usr/bin/caddy /usr/bin/caddy
