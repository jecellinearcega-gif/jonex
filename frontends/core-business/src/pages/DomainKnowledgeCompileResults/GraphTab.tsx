import React from 'react';
import KnowledgeGraphPanel from '@/components/KnowledgeGraphPanel';

interface GraphTabProps {
  kbId: string;
  docId?: string;
  /** 透传给面板：外层已有卡片时去掉面板自带的卡面，避免「卡中卡」 */
  bare?: boolean;
}

const GraphTab: React.FC<GraphTabProps> = ({ kbId, docId, bare }) => {
  return <KnowledgeGraphPanel kbId={kbId} docId={docId} bare={bare} />;
};

export default GraphTab;
