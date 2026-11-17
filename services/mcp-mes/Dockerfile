FROM node:22-slim
ENV NODE_ENV=production

# Gateway stdio -> Streamable HTTP (o server.js original continua falando stdio)
RUN npm install -g supergateway@4.2.2

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY server.js ./
COPY services/ services/
COPY swagger/ swagger/
COPY entrypoint.sh /entrypoint.sh

# config/ é gerado no start a partir do ambiente; nunca entra na imagem
RUN mkdir -p /app/config && chown node /app/config && chmod +x /entrypoint.sh
USER node
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
