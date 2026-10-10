import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Input, Button, Table, Tag, Space, Select, Modal, message } from 'antd';
import './index.scss';
import DataSourceEditModal from './DataSourceEditModal';
import {
  PlusOutlined,
  ApiOutlined,
  ControlOutlined,
  CodeOutlined,
  BarChartOutlined,
  ThunderboltOutlined,
  FileTextOutlined,
  CloudOutlined,
  UploadOutlined,
  FolderOpenOutlined,
  VideoCameraOutlined,
  BellOutlined,
  PictureOutlined,
  SoundOutlined,
  TeamOutlined,
  BugOutlined,
  EyeOutlined,
  SwapOutlined,
} from '@ant-design/icons';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import { useStore } from '@/store';
import type {
  DomainKnowledgeDetail as DomainKnowledgeDetailType,
  DataSourceConfig,
  ActionRule,
  RuleTextSegment,
  OntologyInstanceSummary,
  RelationInstanceSummary,
  OntologyStatistics,
  DomainKnowledgePermissionMember,
  DomainKnowledgePermissionRole,
  KnowledgeBaseType,
  WikiStats,
} from '@/types/domainKnowledge';
import { getStatusTextMap } from '@/types/domainKnowledge';
import {
  getDomainKnowledgeDetail,
  getDomainKnowledgeDataSources,
  getDomainKnowledgeActionRules,
  getOntologyEntityTypes,
  getOntologyRelationTypes,
  getOntologyStatistics,
  getDomainKnowledgePermissions,
  saveDomainKnowledgePermissions,
  getWikiContents,
  type ParserConfigItem,
} from '@/api/domainKnowledge';
import { isLlmWikiType } from '@/utils/kbType';
import CompileTab from './compile/CompileTab';
import LlmWikiSchemaTab from './llmWikiSchema/LlmWikiSchemaTab';
import JonexWikiPromptTab from './llmWikiSchema/JonexWikiPromptTab';
import SynonymTab from './synonym/SynonymTab';
import DataSourceTab from './DataSourceTab';
import ResultTab from './ResultTab';
import ActionTab from './ActionTab';
import PermissionTab from './PermissionTab';
import AddDataSourceModal from '@/components/datasource/AddDataSourceModal';
import AssetStatTag, { buildAssetTags } from '@/components/AssetStatTag';
import { deleteDataSource } from '@/api/dataSource';
import { ParserConfigContent } from '@/pages/DomainKnowledgeParser';

function getTabs(t: (key: string) => string, kbType?: KnowledgeBaseType) {
  const tabs = [
    {
      key: 'datasource',
      label: t('domainKnowledge.tab.datasource'),
      icon: ApiOutlined,
    },
    {
      key: 'parse',
      label: t('domainKnowledge.tab.parse'),
      icon: ControlOutlined,
    },
    {
      key: 'compile',
      label: t('domainKnowledge.tab.compile'),
      icon: CodeOutlined,
    },
    // { key: 'result', label: t('domainKnowledge.tab.result'), icon: BarChartOutlined },
    {
      key: 'synonym',
      label: t('domainKnowledge.tab.synonym'),
      icon: SwapOutlined,
    },
    {
      key: 'permission',
      label: t('domainKnowledge.tab.permission'),
      icon: TeamOutlined,
    },
    // { key: 'action', label: 'Action', icon: ThunderboltOutlined },
  ];
  // [jonex] 同义词只服务本体（lightrag）抽取链路：LLM-Wiki 型（openkb/jonex_wiki）的编译与
  // 检索都不读 ontology_synonyms，tab 留着会让人以为建了就生效 —— 故对该类型隐藏。
  return isLlmWikiType(kbType) ? tabs.filter((tab) => tab.key !== 'synonym') : tabs;
}

export const dataSourceIconMap: Record<string, React.ComponentType<any>> = {
  api: CloudOutlined,
  upload: UploadOutlined,
  storage: FolderOpenOutlined,
};

export const actionIconMap: Record<string, React.ComponentType<any>> = {
  video: VideoCameraOutlined,
  bell: BellOutlined,
  picture: PictureOutlined,
  file: FileTextOutlined,
  sound: SoundOutlined,
  bug: BugOutlined,
  team: TeamOutlined,
};

function renderRuleText(segments: RuleTextSegment[]): React.ReactNode {
  return segments.map((seg, i) =>
    seg.bold ? (
      <strong key={i} style={{ color: seg.color || '#0b2b5c' }}>
        {seg.text}
      </strong>
    ) : (
      <React.Fragment key={i}>{seg.text}</React.Fragment>
    ),
  );
}

const DomainKnowledgeDetail = function DomainKnowledgeDetail() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { global } = useStore();
  const currentSpaceId = global.currentSpaceId;
  const [activeTab, setActiveTab] = useState('datasource');
  const dsStatusLabel = (status: string) => {
    const map: Record<string, string> = {
      运行中: 'domainKnowledge.dataSourceStatus.running',
      同步失败: 'domainKnowledge.dataSourceStatus.failed',
      已暂停: 'domainKnowledge.dataSourceStatus.paused',
    };
    return t(map[status] || status);
  };
  const ruleStatusLabel = (status: string) => {
    const map: Record<string, string> = {
      启用: 'status.active',
      停用: 'status.inactive',
    };
    return t(map[status] || status);
  };
  const permissionMemberName = (member: DomainKnowledgePermissionMember) => member.name;

  // ── detail header ────────────────────────────────────
  const [detail, setDetail] = useState<DomainKnowledgeDetailType | null>(null);
  const [detailLoading, setDetailLoading] = useState(true);
  // LLM-Wiki 的 Wiki 主题/概念不在 KB 详情里（detail 的 entityCount/relationCount 是
  // Wiki 图谱的节点/边计数），得单独取一份全库 Wiki 内容。null = 还没拿到，标签按 0 渲染。
  const [wikiStats, setWikiStats] = useState<WikiStats | null>(null);

  // ── datasource tab ────────────────────────────────────
  const [dataSources, setDataSources] = useState<DataSourceConfig[]>([]);
  const [dataSourcesLoading, setDataSourcesLoading] = useState(false);
  const [addDsOpen, setAddDsOpen] = useState(false);

  // 编辑数据源
  const [editingDs, setEditingDs] = useState<DataSourceConfig | null>(null);

  const openEditModal = (ds: DataSourceConfig) => {
    setEditingDs(ds);
  };

  const reloadDataSources = useCallback(() => {
    if (!id) return;
    setDataSourcesLoading(true);
    getDomainKnowledgeDataSources(id, t)
      .then(setDataSources)
      .catch((err: any) => message.error(err?.message || t('domainKnowledge.fetchDataSourcesFailed')))
      .finally(() => setDataSourcesLoading(false));
  }, [id, t]);

  // ── result tab ────────────────────────────────────────
  const [ontologySummaries, setOntologySummaries] = useState<OntologyInstanceSummary[]>([]);
  const [relationSummaries, setRelationSummaries] = useState<RelationInstanceSummary[]>([]);
  const [resultStats, setResultStats] = useState<OntologyStatistics | null>(null);
  const [resultLoading, setResultLoading] = useState(false);

  // ── action tab ────────────────────────────────────────
  const [actionRules, setActionRules] = useState<ActionRule[]>([]);
  const [actionLoading, setActionLoading] = useState(false);

  // ── permission tab ────────────────────────────────────
  const [permissionMembers, setPermissionMembers] = useState<DomainKnowledgePermissionMember[]>([]);
  const [permissionLoading, setPermissionLoading] = useState(false);
  const [permissionSaving, setPermissionSaving] = useState(false);
  const [permissionKeyword, setPermissionKeyword] = useState('');
  const permDebounceRef = useRef<ReturnType<typeof setTimeout>>();

  const reloadPermissions = useCallback(() => {
    if (!id) return;
    setPermissionLoading(true);
    getDomainKnowledgePermissions(id, permissionKeyword || undefined)
      .then((data) => setPermissionMembers(data.members))
      .catch((err: any) => message.error(err?.message || t('domainKnowledge.fetchPermissionMembersFailed')))
      .finally(() => setPermissionLoading(false));
  }, [id, permissionKeyword, t]);

  const handlePermKeywordChange = (val: string) => {
    setPermissionKeyword(val);
    if (permDebounceRef.current) clearTimeout(permDebounceRef.current);
    permDebounceRef.current = setTimeout(() => {
      if (!id) return;
      setPermissionLoading(true);
      getDomainKnowledgePermissions(id, val || undefined)
        .then((data) => setPermissionMembers(data.members))
        .catch(() => {
          /* silent */
        })
        .finally(() => setPermissionLoading(false));
    }, 300);
  };

  const handlePermAddMember = (user: import('@/types/domainService').MemberCandidate) => {
    setPermissionMembers((prev) => {
      if (prev.some((m) => m.userId === user.user_id)) return prev;
      const name = user.display_name || user.username || user.user_id;
      return [
        ...prev,
        {
          userId: user.user_id,
          name,
          dept: '',
          avatarText: name.charAt(0).toUpperCase(),
          avatarColor: '#94a3b8',
          role: 'member',
        },
      ];
    });
  };

  const handlePermRoleChange = (userId: string, role: DomainKnowledgePermissionRole) => {
    setPermissionMembers((prev) => prev.map((m) => (m.userId === userId ? { ...m, role } : m)));
  };

  const handleSavePermissions = useCallback(async () => {
    if (!id) return;
    setPermissionSaving(true);
    try {
      await saveDomainKnowledgePermissions(id, {
        members: permissionMembers.map((m) => ({
          userId: m.userId,
          role: m.role,
        })),
      });
      message.success(t('domainKnowledge.savePermissionSuccess'));
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.savePermissionFailed'));
    } finally {
      setPermissionSaving(false);
    }
  }, [id, permissionMembers, t]);

  const loadedTabsRef = useRef<Set<string>>(new Set());

  // ── fetch detail ──────────────────────────────────────
  useEffect(() => {
    if (!id) return;
    setDetailLoading(true);
    // [jonex] 设置页需要页头实体/关系徽标，带 with_stats 拉取跨容器全图统计（可接受多等 ~2.6s）。
    getDomainKnowledgeDetail(id, { withStats: true })
      .then(setDetail)
      .catch((err: any) => message.error(err?.message || t('domainKnowledge.fetchDetailFailed')))
      .finally(() => setDetailLoading(false));
  }, [id, t]);

  // ── fetch Wiki 计数（仅 llm-wiki 库）────────────────────
  // 页头标签按库型二选一（见 buildAssetTags），Wiki 那一组要这份数据；kbType 未定时先不动，
  // 拿到后若不是 llm-wiki 就清空——否则切库时会把上个库的页数留在页头上。
  useEffect(() => {
    if (!id || !isLlmWikiType(detail?.kbType)) {
      setWikiStats(null);
      return;
    }
    getWikiContents(id, '')
      .then((res) =>
        setWikiStats({
          summaries: res.summaries?.length ?? 0,
          concepts: res.concepts?.length ?? 0,
          entities: res.entities?.length ?? 0,
        }),
      )
      .catch(() => setWikiStats(null));
  }, [id, detail?.kbType]);

  // ── load tab data lazily ──────────────────────────────
  const loadTabData = useCallback(
    (tabKey: string) => {
      if (!id || loadedTabsRef.current.has(tabKey)) return;
      loadedTabsRef.current.add(tabKey);

      switch (tabKey) {
        case 'datasource':
          setDataSourcesLoading(true);
          getDomainKnowledgeDataSources(id, t)
            .then(setDataSources)
            .catch((err: any) => message.error(err?.message || t('domainKnowledge.fetchDataSourcesFailed')))
            .finally(() => setDataSourcesLoading(false));
          break;

        case 'result':
          setResultLoading(true);
          Promise.all([getOntologyStatistics(id), getOntologyEntityTypes(id), getOntologyRelationTypes(id)])
            .then(([stats, entityTypes, relationTypes]) => {
              setResultStats(stats);
              setOntologySummaries(entityTypes.items);
              setRelationSummaries(relationTypes.items);
            })
            .catch((err: any) => message.error(err?.message || t('domainKnowledge.fetchResultDataFailed')))
            .finally(() => setResultLoading(false));
          break;

        case 'action':
          setActionLoading(true);
          getDomainKnowledgeActionRules(id)
            .then(setActionRules)
            .catch((err: any) => message.error(err?.message || t('domainKnowledge.fetchActionRulesFailed')))
            .finally(() => setActionLoading(false));
          break;

        case 'permission':
          setPermissionLoading(true);
          getDomainKnowledgePermissions(id)
            .then((data) => setPermissionMembers(data.members))
            .catch((err: any) => message.error(err?.message || t('domainKnowledge.fetchPermissionMembersFailed')))
            .finally(() => setPermissionLoading(false));
          break;
      }
    },
    [id, t],
  );

  useEffect(() => {
    loadTabData(activeTab);
  }, [activeTab, loadTabData]);

  // ── render helpers ────────────────────────────────────
  const renderSectionHeader = (title: string, icon: React.ReactNode, showAdd = true) => (
    <div className="yx-kb-flex-header">
      <h3 className="yx-kb-section-title">
        {icon} {title}
      </h3>
      {showAdd && (
        <Button type="primary" className="yx-kb-section-add-btn" icon={<PlusOutlined />}>
          {t('domainKnowledge.newWithTitle', {
            title: title.replace('设置', '').replace('管理', ''),
          })}
        </Button>
      )}
    </div>
  );

  // ── render tab content ────────────────────────────────
  const renderTabContent = () => {
    switch (activeTab) {
      case 'datasource':
        return (
          <DataSourceTab
            dataSources={dataSources}
            dataSourcesLoading={dataSourcesLoading}
            canWrite={detail?.can_write ?? false}
            onAdd={() => setAddDsOpen(true)}
            onEdit={(ds) => openEditModal(ds)}
            onReload={reloadDataSources}
          />
        );

      case 'parse':
        return <ParserConfigContent kbId={id} spaceId={currentSpaceId} canWrite={detail?.can_write ?? false} />;

      case 'compile':
        // [jonex] 管线分流（方案 §10）三引擎显式：
        // jonex_wiki → compile_main prompt 编辑；openkb → LLM-Wiki Schema（词表）；
        // lightrag → 本体 CompileTab。detail?.kbType 随 KB 详情加载；未到达前按 lightrag。
        if (detail?.kbType === 'jonex_wiki')
          return <JonexWikiPromptTab kbId={id!} canWrite={detail?.can_write ?? false} />;
        if (detail?.kbType === 'openkb')
          return <LlmWikiSchemaTab kbId={id!} canWrite={detail?.can_write ?? false} />;
        return <CompileTab kbId={id!} canWrite={detail?.can_write ?? false} />;

      case 'synonym':
        return <SynonymTab kbId={id!} canWrite={detail?.can_write ?? false} />;

      case 'result':
        return (
          <ResultTab
            resultStats={resultStats}
            ontologySummaries={ontologySummaries}
            relationSummaries={relationSummaries}
            resultLoading={resultLoading}
          />
        );

      case 'action':
        return (
          <ActionTab
            actionRules={actionRules}
            actionLoading={actionLoading}
            ruleStatusLabel={ruleStatusLabel}
            renderRuleText={renderRuleText}
          />
        );

      case 'permission':
        return (
          <PermissionTab
            kbId={id}
            members={permissionMembers}
            loading={permissionLoading}
            saving={permissionSaving}
            canManage={detail?.can_manage_permissions ?? false}
            memberName={permissionMemberName}
            onAdd={handlePermAddMember}
            onRoleChange={handlePermRoleChange}
            onRemove={(userId) => setPermissionMembers((prev) => prev.filter((m) => m.userId !== userId))}
            onSave={handleSavePermissions}
          />
        );

      default:
        return null;
    }
  };

  // 本页有两个入口——知识库列表的下拉菜单、知识库详情页 tab 栏的「知识库设置」按钮。
  // 返回目标由来源决定：入口在 navigate 时带上 backTo，这里读出来用。
  // 直接打开链接（无 state）时回落到知识库列表，与加 state 之前的行为一致。
  const backState = location.state as { backTo?: string } | null;
  const backTo = backState?.backTo || '/domain-knowledge';

  // 页头三个标签：与知识库详情页共用 buildAssetTags，两处页头永远同款（口径见该函数注释）
  const assetTags = detail ? buildAssetTags(t, detail, wikiStats) : [];

  // ── main render ──
  return (
    <>
      {/* 页头承担返回与 KB 身份：返回按钮、名称/统计、同步状态排在同一行 */}
      <PageHeader
        // backTo 传函数：本页返回目标由来源决定（见上），不是固定路径
        backTo={() => navigate(backTo)}
        // 不传 backText：返回按钮只留箭头图标（PageHeader 不传即只渲染图标）
        title={
          <span className="yx-kb-header-title">
            <span className="yx-kb-header-name">
              {detailLoading ? t('common.loading') : detail?.name}
            </span>
            {/* 统计标签随详情一起出现：加载中与未取到时它没有可信数值可显示 */}
            {detail && (
              <span className="yx-kb-tag-row">
                {assetTags.map((tag) => (
                  <AssetStatTag
                    key={tag.label}
                    label={tag.label}
                    value={tag.value}
                    icon={tag.icon}
                    tone={tag.tone}
                  />
                ))}
              </span>
            )}
          </span>
        }
        extra={
          detail && (
            <span className="yx-kb-header-status">
              <span className={`yx-kb-detail-status ${detail.status}`}>{getStatusTextMap(t)[detail.status]}</span>
              <span className="yx-kb-header-updated">
                {t('domainKnowledge.lastUpdated', { time: detail.updatedAt })}
              </span>
            </span>
          )
        }
      />

      <PageContent>
        {/* Tab Bar */}
        <div className="yx-kb-detail-tabs">
          {getTabs(t, detail?.kbType).map((tab) => {
            const Icon = tab.icon;
            const active = activeTab === tab.key;
            return (
              <div
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                className={`yx-kb-detail-tab${active ? ' active' : ''}`}
              >
                <Icon /> {tab.label}
              </div>
            );
          })}
        </div>

        {renderTabContent()}
      </PageContent>

      {/* 编辑数据源弹窗 */}
      <DataSourceEditModal
        editingDs={editingDs}
        onClose={() => setEditingDs(null)}
        onSaved={() => {
          setEditingDs(null);
          reloadDataSources();
        }}
      />

      <AddDataSourceModal
        open={addDsOpen}
        kbId={id!}
        existingTypes={dataSources.map((ds) => ds.accessType)}
        onClose={() => setAddDsOpen(false)}
        onCreated={() => {
          setAddDsOpen(false);
          reloadDataSources();
        }}
      />
    </>
  );
};

export default DomainKnowledgeDetail;
