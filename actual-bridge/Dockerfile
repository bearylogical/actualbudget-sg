FROM node:20-alpine

WORKDIR /app
COPY package.json .
# Install @actual-app/api at latest to match your server.
# If you get out-of-sync-migrations, pin this to match your server's version:
# e.g. change "latest" to "25.10.0" in package.json
RUN npm install

COPY index.js .
RUN mkdir -p /data/actual

EXPOSE 3001
CMD ["node", "--import", "./polyfill.js", "index.js"]
