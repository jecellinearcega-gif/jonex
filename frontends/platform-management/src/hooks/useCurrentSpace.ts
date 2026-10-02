import { useEffect, useState } from 'react';
import { onSpaceChanged, readPersistedSpaceId } from '@jonex/shell-sdk';

/**
 * 当前领域空间 id，未选空间为 null（即全平台口径）。
 *
 * 同窗口 CustomEvent 与跨 iframe/跨标签页 storage 事件都由 shell-sdk 收敛，
 * 所以 hosted 与 standalone 两种形态下都能收到切换通知。
 */
export default function useCurrentSpace(): string | null {
  const [spaceId, setSpaceId] = useState<string | null>(() => {
    try {
      return readPersistedSpaceId();
    } catch {
      // localStorage 被禁用时退回全平台口径，不让整页取数崩掉
      return null;
    }
  });

  useEffect(() => onSpaceChanged(setSpaceId), []);

  return spaceId;
}
