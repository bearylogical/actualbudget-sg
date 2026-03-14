import { sveltekit } from '@sveltejs/kit/vite';

export default {
  plugins: [sveltekit()],
  server: {
    proxy: {
      '/api': {
        target: 'http://backend:8000',
        rewrite: path => path.replace(/^\/api/, '')
      }
    }
  }
};
