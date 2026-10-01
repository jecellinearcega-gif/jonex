/// <reference types="vite/client" />

declare module '*.module.scss' {
  const classes: { readonly [key: string]: string };
  export default classes;
}

declare module '*.scss' {
  const content: string;
  export default content;
}

declare module '@loadable/component' {
  import type { ComponentType } from 'react';
  const loadable: (loadFn: () => Promise<{ default: ComponentType<any> }>) => ComponentType<any>;
  export default loadable;
}
