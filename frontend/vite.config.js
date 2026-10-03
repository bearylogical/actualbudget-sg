import { sveltekit } from '@sveltejs/kit/vite';

export default {
  plugins: [sveltekit()],
  server: {
    proxy: {
      '/api': {
        target: process.env.API_TARGET || 'http://backend:8000',
        rewrite: path => path.replace(/^\/api/, '')
      }
    }
  }
};
