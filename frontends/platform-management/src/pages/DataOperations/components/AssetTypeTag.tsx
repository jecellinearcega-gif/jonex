import { Tag } from 'antd';
import type { DoAssetType } from '../types';

/** 类型配色走 CSS 类（index.scss 的 .yx-do-type-tag--*），不内联 style：
 *  六个类型的对象若内联会重复出现在三处表格里 */
export default function AssetTypeTag({ type, label }: { type: DoAssetType; label: string }) {
  return (
    <Tag variant="filled" className={`yx-do-type-tag yx-do-type-tag--${type}`}>
      {label}
    </Tag>
  );
}
