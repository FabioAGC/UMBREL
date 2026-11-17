FROM caddy:2-alpine

# Configuração e páginas vão DENTRO da imagem (no Umbrel, bind mounts não persistem).
COPY Caddyfile /etc/caddy/Caddyfile
COPY site/ /srv/
