import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Input, Select, Button, Card, Switch, InputNumber, Divider, Dropdown, Modal } from 'antd';
import { SearchOutlined, StopOutlined, SettingOutlined, DownOutlined } from '@ant-design/icons';
import classNames from 'classnames';
import type { KnowledgeSearchDomain, KnowledgeSearchStrictConfig, KnowledgeSearchUiMode } from '@/types/knowledgeSearch';
import { DEFAULT_FAST_STRICT_CONFIG, DEFAULT_DEEP_STRICT_CONFIG, DEFAULT_WIKI_STRICT_CONFIG } from '@/api/knowledgeSearch';
import styles from './SearchPanel.module.scss';

/** 档位元数据：label/desc 的 i18n key + 是否支持严格模式配置 */
const MODE_META: { mode: KnowledgeSearchUiMode; labelKey: string; descKey: string; configurable: boolean }[] = [
  { mode: 'fast', labelKey: 'knowledgeSearch.fastSearch', descKey: 'knowledgeSearch.fastSearchDesc', configurable: true },
  { mode: 'deep', labelKey: 'knowledgeSearch.deepSearch', descKey: 'knowledgeSearch.deepSearchDesc', configurable: true },
  // Wiki 检索：走 jonex_wiki 专用端点，该端点不接受严格模式参数，故不给配置入口
  { mode: 'wiki', labelKey: 'knowledgeSearch.wikiSearch', descKey: 'knowledgeSearch.wikiSearchDesc', configurable: false },
];

const DEFAULT_CONFIG_BY_MODE: Record<KnowledgeSearchUiMode, KnowledgeSearchStrictConfig> = {
  fast: DEFAULT_FAST_STRICT_CONFIG,
  deep: DEFAULT_DEEP_STRICT_CONFIG,
  wiki: DEFAULT_WIKI_STRICT_CONFIG,
};

interface SearchPanelProps {
  selectedDomain: string;
  visibleDomains: KnowledgeSearchDomain[];
  query: string;
  isSearching: boolean;
  /** 检索模式：快速 / 深度 / Wiki（Wiki 走 /search/jonex_wiki 专用端点） */
  searchMode: KnowledgeSearchUiMode;
  onSearchModeChange: (mode: KnowledgeSearchUiMode) => void;
  /** 严格模式配置 */
  strictConfig: KnowledgeSearchStrictConfig;
  onStrictConfigChange: (config: KnowledgeSearchStrictConfig) => void;
  onDomainChange: (value: string) => void;
  onQueryChange: (value: string) => void;
  onSearch: () => void;
  onStop: () => void;
}

interface ConfigFieldProps {
  label: string;
  description: string;
  children: React.ReactNode;
}

function ConfigField({ label, description, children }: ConfigFieldProps) {
  return (
    <div className={styles.configField}>
      <div className={styles.configFieldInfo}>
        <div className={styles.configFieldLabel}>{label}</div>
        <div className={styles.configFieldDesc}>{description}</div>
      </div>
      <div className={styles.configFieldControl}>{children}</div>
    </div>
  );
}

export default function SearchPanel({
  selectedDomain,
  visibleDomains,
  query,
  isSearching,
  searchMode,
  onSearchModeChange,
  onStrictConfigChange,
  onDomainChange,
  onQueryChange,
  onSearch,
  onStop,
}: SearchPanelProps) {
  const { t } = useTranslation();

  // [jonex] 各档位各自保留一份严格模式配置：切档位时把该档位的配置回传给页面（当前生效配置）
  const [modeConfigs, setModeConfigs] =
    useState<Record<KnowledgeSearchUiMode, KnowledgeSearchStrictConfig>>(DEFAULT_CONFIG_BY_MODE);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingMode, setEditingMode] = useState<KnowledgeSearchUiMode>('fast');
  const [draftConfig, setDraftConfig] = useState<KnowledgeSearchStrictConfig>(DEFAULT_FAST_STRICT_CONFIG);
  const [dropdownOpen, setDropdownOpen] = useState(false);

  const activeMeta = MODE_META.find((m) => m.mode === searchMode) ?? MODE_META[0];
  const activeModeLabel = t(activeMeta.labelKey);
  const editingMeta = MODE_META.find((m) => m.mode === editingMode) ?? MODE_META[0];
  const editingModeLabel = t(editingMeta.labelKey);

  const switchMode = (next: KnowledgeSearchUiMode) => {
    if (next === searchMode) {
      setDropdownOpen(false);
      return;
    }
    onSearchModeChange(next);
    onStrictConfigChange(modeConfigs[next]);
    setDropdownOpen(false);
  };

  const openConfigModal = (mode: KnowledgeSearchUiMode) => {
    setEditingMode(mode);
    setDraftConfig(modeConfigs[mode]);
    setModalOpen(true);
    setDropdownOpen(false);
  };

  const saveConfig = () => {
    setModeConfigs((prev) => ({ ...prev, [editingMode]: draftConfig }));
    if (editingMode === searchMode) {
      onStrictConfigChange(draftConfig);
    }
    setModalOpen(false);
  };

  const updateDraft = (patch: Partial<KnowledgeSearchStrictConfig>) => {
    setDraftConfig((prev) => ({ ...prev, ...patch }));
  };

  const dropdownContent = (
    <div className={styles.modeDropdown}>
      {MODE_META.map((item, index) => (
        <React.Fragment key={item.mode}>
          {index > 0 && <Divider style={{ margin: 0 }} />}
          <div
            className={classNames(styles.modeOption, { [styles.modeOptionActive]: searchMode === item.mode })}
            onClick={() => switchMode(item.mode)}
            role="button"
            tabIndex={0}
          >
            <div className={styles.modeOptionContent}>
              <div className={styles.modeOptionTitle}>{t(item.labelKey)}</div>
              <div className={styles.modeOptionDesc}>{t(item.descKey)}</div>
            </div>
            {item.configurable && (
              <Button
                type="text"
                size="small"
                icon={<SettingOutlined />}
                className={styles.modeOptionSetting}
                onClick={(e) => {
                  e.stopPropagation();
                  openConfigModal(item.mode);
                }}
                title={t('knowledgeSearch.strictConfigTitle')}
              />
            )}
          </div>
        </React.Fragment>
      ))}
    </div>
  );

  return (
    <Card style={{ borderRadius: 16, marginBottom: 24 }} styles={{ body: { padding: '24px 28px' } }}>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        <Select
          value={selectedDomain}
          onChange={onDomainChange}
          style={{ minWidth: 160, height: 48 }}
          size="large"
          options={visibleDomains.map((d) => ({
            value: d.id,
            // 当前领域空间（kind=space）为返回首项，label 固定显示「当前领域空间」
            label: d.kind === 'space' ? t('knowledgeSearch.currentSpace') : d.name,
          }))}
        />
        <Input
          prefix={<SearchOutlined style={{ color: '#94a3b8', fontSize: 16 }} />}
          placeholder={t('knowledgeSearch.searchPlaceholder')}
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          onPressEnter={onSearch}
          style={{ height: 48, fontSize: 15, background: '#f8fafc', borderRadius: 10 }}
        />
        {isSearching && (
          <Button
            icon={<StopOutlined />}
            onClick={onStop}
            style={{ height: 48, padding: '0 18px', fontSize: 15, borderRadius: 10, flexShrink: 0 }}
          >
            {t('knowledgeSearch.stop')}
          </Button>
        )}
        <div className={styles.modeSelector}>
          <Button
            type="primary"
            icon={<SearchOutlined />}
            onClick={onSearch}
            loading={isSearching}
            className={styles.modeSearchButton}
          >
            {activeModeLabel}
          </Button>
          <Dropdown
            open={dropdownOpen}
            onOpenChange={setDropdownOpen}
            trigger={['click']}
            placement="bottomRight"
            menu={{ items: [] }}
            popupRender={() => dropdownContent}
          >
            <Button type="primary" icon={<DownOutlined />} className={styles.modeDropdownTrigger} disabled={isSearching} />
          </Dropdown>
        </div>
      </div>

      <Modal
        title={`${editingModeLabel} · ${t('knowledgeSearch.strictConfigTitle')}`}
        open={modalOpen}
        onOk={saveConfig}
        onCancel={() => setModalOpen(false)}
        okText={t('knowledgeSearch.saveSettings')}
        cancelText={t('knowledgeSearch.cancel')}
        width={560}
      >
        <div className={styles.configIntro}>{t('knowledgeSearch.strictConfigDescription')}</div>
        <div className={styles.configForm}>
          <ConfigField
            label={t('knowledgeSearch.strictMode')}
            description={t('knowledgeSearch.strictModeDesc')}
          >
            <Switch checked={draftConfig.strict_mode} onChange={(v) => updateDraft({ strict_mode: v })} />
          </ConfigField>
          {draftConfig.strict_mode && (
            <>
              <ConfigField
                label={t('knowledgeSearch.strictMaxAttempts')}
                description={t('knowledgeSearch.strictMaxAttemptsDesc')}
              >
                <InputNumber
                  min={0}
                  max={10}
                  value={draftConfig.strict_max_attempts}
                  onChange={(v) => updateDraft({ strict_max_attempts: v ?? 0 })}
                  className={styles.configNumber}
                />
              </ConfigField>
              <ConfigField
                label={t('knowledgeSearch.strictMinScore')}
                description={t('knowledgeSearch.strictMinScoreDesc')}
              >
                <InputNumber
                  min={0}
                  max={1}
                  step={0.05}
                  value={draftConfig.strict_min_score}
                  onChange={(v) => updateDraft({ strict_min_score: v ?? 0 })}
                  className={styles.configNumber}
                />
              </ConfigField>
              <ConfigField
                label={t('knowledgeSearch.strictRequireReference')}
                description={t('knowledgeSearch.strictRequireReferenceDesc')}
              >
                <Switch
                  checked={draftConfig.strict_require_reference}
                  onChange={(v) => updateDraft({ strict_require_reference: v })}
                />
              </ConfigField>
              <ConfigField
                label={t('knowledgeSearch.strictRequireGrounded')}
                description={t('knowledgeSearch.strictRequireGroundedDesc')}
              >
                <Switch
                  checked={draftConfig.strict_require_grounded}
                  onChange={(v) => updateDraft({ strict_require_grounded: v })}
                />
              </ConfigField>
              {editingMode === 'deep' && (
                <>
                  <ConfigField
                    label={t('knowledgeSearch.maxSubqueries')}
                    description={t('knowledgeSearch.maxSubqueriesDesc')}
                  >
                    <InputNumber
                      min={1}
                      max={20}
                      value={draftConfig.max_subqueries}
                      onChange={(v) => updateDraft({ max_subqueries: v ?? 1 })}
                      className={styles.configNumber}
                    />
                  </ConfigField>
                  <ConfigField
                    label={t('knowledgeSearch.allowCommonSense')}
                    description={t('knowledgeSearch.allowCommonSenseDesc')}
                  >
                    <Switch
                      checked={draftConfig.allow_common_sense}
                      onChange={(v) => updateDraft({ allow_common_sense: v })}
                    />
                  </ConfigField>
                </>
              )}
            </>
          )}
        </div>
      </Modal>
    </Card>
  );
}
