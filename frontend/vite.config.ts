import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  base: '/',
  publicDir: 'public',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
    assetsDir: 'assets',
    assetsInlineLimit: 0,
    sourcemap: false,
    manifest: true,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/codey-[hash].js',
        chunkFileNames: 'assets/codey-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
        manualChunks(id) {
          if (!id.includes('/node_modules/')) return undefined;
          if (id.includes('/katex/')) return 'vendor-katex';
          if (id.includes('/markdown-it') || id.includes('/markdown-it-task-lists/')) return 'vendor-markdown';
          return undefined;
        },
      }
    }
  }
});
