import { build, defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  base: '/',
  publicDir: 'public',
  plugins: [{
    name: 'desktop-startup',
    transformIndexHtml: {
      order: 'post',
      handler() {
        return [
          { tag: 'link', attrs: { rel: 'stylesheet', href: '/assets/desktop-startup.css' }, injectTo: 'head' },
          { tag: 'script', attrs: { src: '/assets/desktop-startup.js' }, injectTo: 'body-prepend' },
        ];
      },
    },
    async generateBundle() {
      // One animation implementation, also usable before the HTTP service exists.
      const result = await build({
        configFile: false, publicDir: false,
        build: {
          write: false, target: 'es2022', minify: true,
          lib: { entry: 'src/desktop_splash.ts', name: 'CodeyStartup', formats: ['iife'] },
        },
      });
      for (const bundle of Array.isArray(result) ? result : [result]) {
        if (!('output' in bundle)) throw new Error('Startup build did not produce output');
        for (const item of bundle.output) {
          this.emitFile({ type: 'asset', fileName: `assets/desktop-startup.${item.type === 'chunk' ? 'js' : 'css'}`,
            source: item.type === 'chunk' ? item.code : item.source });
        }
      }
    },
  }],
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
