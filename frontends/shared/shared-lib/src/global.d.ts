declare module '@loadable/component' {
  import type { ComponentType } from 'react';
  const loadable: (loadFn: () => Promise<{ default: ComponentType<any> }>) => ComponentType<any>;
  export default loadable;
}
