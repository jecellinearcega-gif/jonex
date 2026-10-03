import { useState, useEffect, useMemo, useCallback } from 'react';
import { Button, Card, Input, Select, Tag, message, Spin, Result, Space, Typography } from 'antd';
import { SearchOutlined, EyeOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { colors } from '@jonex/platform-theme/tokens';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import { fetchSkills, enableSkill, disableSkill, type SkillItem, type FetchSkillsParams } from '../../api/skills';
import { CATEGORY_ICONS, CATEGORY_LABEL_KEYS, BUILT_IN_SKILL_KEYS } from './shared';
import SkillDetailModal from './SkillDetailModal';
import './index.css';
const { Paragraph } = Typography;

const CATEGORY_OPTIONS = ['image', 'voice', 'document', 'video', 'fusion', 'custom'] as const;

const CATEGORY_GRADIENTS: Record<string, string> = {
  image: 'linear-gradient(135deg, #3b82f6, #1d4ed8)',
  voice: 'linear-gradient(135deg, #10b981, #059669)',
  document: 'linear-gradient(135deg, #f97316, #ea580c)',
  video: 'linear-gradient(135deg, #8b5cf6, #7c3aed)',
  fusion: 'linear-gradient(135deg, #ef4444, #dc2626)',
  custom: 'linear-gradient(135deg, #06b6d4, #0891b2)',
};

const BUILT_IN_SKILL_TAGS: Record<string, string[]> = {
  imageRecognition: ['image', 'ocr', 'recognition'],
  speechToText: ['speech', 'transcription', 'asr'],
  documentLayout: ['document', 'layout', 'structured'],
  videoUnderstanding: ['video', 'frameSampling', 'analysis'],
  multimodalSearch: ['search', 'fusion', 'semantic'],
  dataExtraction: ['extraction', 'structured', 'document'],
};

function getSkillCopy(skill: SkillItem, t: (key: string) => string) {
  const key = BUILT_IN_SKILL_KEYS[skill.id];
  if (!key) {
    return {
      name: skill.name,
      description: skill.description || '',
      instruction: skill.instruction,
      tags: skill.tags,
    };
  }
  return {
    name: t(`skills.builtIn.${key}.name`),
    description: t(`skills.builtIn.${key}.description`),
    instruction: t(`skills.builtIn.${key}.instruction`),
    tags: (BUILT_IN_SKILL_TAGS[key] || []).map((tag) => t(`skills.tags.${tag}`)),
  };
}

export default function Skills() {
  const { t } = useTranslation();
  const [items, setItems] = useState<SkillItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [keyword, setKeyword] = useState('');
  const [category, setCategory] = useState<string | undefined>();
  const [pagination, setPagination] = useState({ offset: 0, limit: 20, total: 0 });
  const [submittingId, setSubmittingId] = useState<string | null>(null);
  const [detailSkill, setDetailSkill] = useState<SkillItem | null>(null);

  const loadSkills = useCallback(
    async (next?: FetchSkillsParams) => {
      setLoading(true);
      setError(null);
      try {
        const params = next ?? { offset: 0, limit: 20, keyword, category };
        const result = await fetchSkills(params);
        setItems(result.items);
        setPagination({
          offset: result.offset ?? 0,
          limit: result.limit ?? 20,
          total: result.total,
        });
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : t('skills.loadFailed'));
      } finally {
        setLoading(false);
      }
    },
    [keyword, category],
  );

  useEffect(() => {
    loadSkills();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const filtered = useMemo(() => {
    return items.filter((s) => {
      const display = getSkillCopy(s, t);
      if (keyword) {
        const q = keyword.toLowerCase();
        if (
          !s.name.toLowerCase().includes(q) &&
          !display.name.toLowerCase().includes(q) &&
          !(s.description || '').toLowerCase().includes(q) &&
          !display.description.toLowerCase().includes(q) &&
          !s.tool_name.toLowerCase().includes(q)
        ) {
          return false;
        }
      }
      if (category && s.category !== category) return false;
      return true;
    });
  }, [items, keyword, category, t]);

  async function handleToggle(skill: SkillItem) {
    setSubmittingId(skill.id);
    try {
      if (skill.enabled) {
        await disableSkill(skill.id);
        message.success(t('skills.disabledSkill'));
      } else {
        await enableSkill(skill.id);
        message.success(t('skills.enabledSkill'));
      }
      await loadSkills({ offset: pagination.offset, limit: pagination.limit, keyword, category });
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.operationFailed'));
    } finally {
      setSubmittingId(null);
    }
  }

  const reload = () => loadSkills({ offset: 0, limit: 20, keyword, category });

  // ── Loading ──
  if (loading && items.length === 0) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 300 }}>
        <Spin size="large" />
      </div>
    );
  }

  // ── Error ──
  if (error) {
    return (
      <Result
        status="error"
        title={t('common.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={reload}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  // ── Content ──
  return (
    <>
      <PageHeader backTo="/" title={t('skills.pageTitle')} style={{ marginBottom: 4 }} />
      <p style={{ color: colors.textMuted, margin: '0 0 16px', fontSize: 14 }}>{t('skills.pageSubtitle')}</p>

      <PageContent>
        {/* 工具栏 */}
        <div
          className="yx-toolbar"
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            marginBottom: 20,
            marginTop: 8,
          }}
        >
          <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
            <Input
              placeholder={t('skills.searchPlaceholder')}
              prefix={<SearchOutlined />}
              value={keyword}
              onChange={(e) => {
                setKeyword(e.target.value);
                setPagination((prev) => ({ ...prev, offset: 0 }));
              }}
              allowClear
              style={{ width: 240 }}
            />
            <Select
              placeholder={t('skills.allCategories')}
              value={category}
              onChange={(v) => {
                setCategory(v);
                setPagination((prev) => ({ ...prev, offset: 0 }));
              }}
              allowClear
              style={{ width: 140 }}
              options={CATEGORY_OPTIONS.map((c) => ({ label: t(CATEGORY_LABEL_KEYS[c]), value: c }))}
            />
          </div>
          <span style={{ fontSize: 13, color: colors.textMuted }}>
            {t('skills.countText', { count: pagination.total })}
          </span>
        </div>

        {/* 空状态 */}
        {filtered.length === 0 ? (
          <div className="yx-empty-state">
            <p>{t('skills.emptyText')}</p>
            <Button onClick={reload}>{t('common.refresh')}</Button>
          </div>
        ) : (
          /* 卡片网格 */
          <div className="skills-grid">
            {filtered.map((skill) => {
              const isBusy = submittingId === skill.id;
              const display = getSkillCopy(skill, t);

              return (
                <Card
                  key={skill.id}
                  hoverable
                  actions={[
                    <Button
                      key="toggle"
                      type={skill.enabled ? 'default' : 'primary'}
                      size="small"
                      loading={isBusy}
                      danger={skill.enabled}
                      onClick={() => handleToggle(skill)}
                    >
                      {skill.enabled ? t('status.inactive') : t('status.active')}
                    </Button>,
                    <Button
                      key="detail"
                      type="text"
                      size="small"
                      icon={<EyeOutlined />}
                      onClick={() => setDetailSkill(skill)}
                    >
                      {t('skills.detailBtn')}
                    </Button>,
                  ]}
                >
                  <Card.Meta
                    avatar={
                      <div
                        style={{
                          width: 48,
                          height: 48,
                          borderRadius: 12,
                          background: CATEGORY_GRADIENTS[skill.category] || CATEGORY_GRADIENTS.custom,
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          fontSize: 22,
                          color: '#fff',
                        }}
                      >
                        {CATEGORY_ICONS[skill.category] || CATEGORY_ICONS.custom}
                      </div>
                    }
                    title={
                      <Space>
                        <span>{display.name}</span>
                        <Tag color={skill.enabled ? 'green' : 'default'}>
                          {skill.enabled ? t('skills.enabled') : t('skills.disabled')}
                        </Tag>
                      </Space>
                    }
                    description={
                      <div>
                        <Paragraph
                          ellipsis={{ rows: 2 }}
                          style={{ marginBottom: 8, color: colors.textSecondary, fontSize: 13 }}
                        >
                          {display.description || t('common.noDescription')}
                        </Paragraph>
                        <Space size={[4, 4]} wrap style={{ marginBottom: 4 }}>
                          <Tag color="blue">{t(CATEGORY_LABEL_KEYS[skill.category]) || skill.category}</Tag>
                          <Tag>{skill.tool_name}</Tag>
                          {display.tags.map((tag) => (
                            <Tag key={tag} style={{ fontSize: 11 }}>
                              {tag}
                            </Tag>
                          ))}
                        </Space>
                      </div>
                    }
                  />
                </Card>
              );
            })}
          </div>
        )}
      </PageContent>

      <SkillDetailModal open={!!detailSkill} skill={detailSkill} onClose={() => setDetailSkill(null)} />
    </>
  );
}
