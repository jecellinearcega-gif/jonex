import React from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Button, Card } from 'antd';
import { LockOutlined, StopOutlined, UserDeleteOutlined } from '@ant-design/icons';
import { colors, radius } from '@jonex/platform-theme/tokens';
import {
  buildBackToLoginUrl,
  resolveSsoErrorContent,
  type SsoErrorIconKey,
} from './ssoErrorModel';

const ICONS: Record<SsoErrorIconKey, React.ReactNode> = {
  stop: <StopOutlined />,
  lock: <LockOutlined />,
  userDelete: <UserDeleteOutlined />,
};

function logoSrc(lng: string) {
  return lng === 'en' ? '/logo-en.svg' : '/logo.svg';
}

/**
 * SSO「无访问权限」错误页。
 *
 * 由后端 /oidc/callback 在「IdP 认证通过、平台侧不放行」时 302 过来，**用户此刻没有登录态**，
 * 因此本路由挂在 App.tsx 里 RequireAuth 之外（放进去会被立刻踢回 /login，页面永远看不到）。
 *
 * 只认 URL 上的两个参数：reason（决定文案/图标）与 tenant（底部小字 + 返回登录页时预填）。
 * reason 未知或缺失一律走兜底文案，不白屏。
 */
function SsoErrorPage() {
  const { t, i18n } = useTranslation();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();

  const content = resolveSsoErrorContent(searchParams.get('reason'));
  // trim 后收口（与 buildBackToLoginUrl 同口径）：否则 `?tenant=%20` 这类输入会渲染出一行
  // 空白小字，而按钮却又按「无租户」跳 /login —— 两处判断必须一致。
  const tenant = searchParams.get('tenant')?.trim() || null;

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        background: colors.bg,
        fontFamily:
          '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif',
      }}
    >
      <Card
        style={{
          width: 440,
          boxShadow: '0 4px 24px rgba(11, 43, 92, 0.08)',
          borderRadius: radius.card,
          border: `1px solid ${colors.borderLight}`,
        }}
        styles={{ body: { padding: '32px 40px 24px', textAlign: 'center' } }}
      >
        <div style={{ marginBottom: 16, textAlign: 'center' }}>
          <img src={logoSrc(i18n.language)} alt={t('site.title')} style={{ height: 40 }} />
        </div>

        <div
          style={{
            width: 64,
            height: 64,
            borderRadius: '50%',
            background: content.iconBg,
            color: content.iconColor,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 30,
            margin: '0 auto 18px',
          }}
        >
          {ICONS[content.iconKey]}
        </div>

        <h1
          style={{
            fontSize: 20,
            fontWeight: 600,
            color: colors.textPrimary,
            margin: '0 0 10px',
          }}
        >
          {t(content.titleKey)}
        </h1>

        <p
          style={{
            fontSize: 13.5,
            lineHeight: 1.75,
            color: colors.textSecondary,
            margin: '0 0 20px',
          }}
        >
          {t(content.descKey)}
        </p>

        <div
          style={{
            background: colors.rowHover,
            borderRadius: radius.btn,
            padding: '12px 14px',
            fontSize: 12.5,
            lineHeight: 1.65,
            color: colors.textSecondary,
            textAlign: 'left',
            marginBottom: 22,
          }}
        >
          <span style={{ color: colors.textMuted }}>{t('auth.ssoError.reasonLabel')}</span>
          {t(content.reasonKey)}
        </div>

        <Button
          type="primary"
          block
          onClick={() => navigate(buildBackToLoginUrl(tenant))}
          style={{
            height: 42,
            borderRadius: radius.btn,
            fontSize: 14.5,
            fontWeight: 500,
          }}
        >
          {t('auth.ssoError.backToLogin')}
        </Button>

        {tenant && (
          <div
            style={{
              marginTop: 22,
              paddingTop: 14,
              borderTop: `1px solid ${colors.borderLight}`,
              fontSize: 11.5,
              color: colors.textMuted,
            }}
          >
            {`${t('auth.ssoError.tenantLabel')} ${tenant}`}
          </div>
        )}
      </Card>
    </div>
  );
}

export default SsoErrorPage;
