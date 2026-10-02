import { redirect } from 'react-router-dom';
import { isEmbedded } from '@jonex/shell-sdk';
import { ErrorPage, NotFound, loadableComponent } from '@jonex/shared-lib';

const BasicLayout = loadableComponent(() => import('@/components/BasicLayout'));
const HostedLayout = loadableComponent(() => import('@/components/HostedLayout'));
const Home = loadableComponent(() => import('@/pages/Home'));
const ModelAdapter = loadableComponent(() => import('@/pages/ModelAdapter'));
const TenantManagement = loadableComponent(() => import('@/pages/TenantManagement'));
const TenantDetail = loadableComponent(() => import('@/pages/TenantDetail'));
const UserManagement = loadableComponent(() => import('@/pages/UserManagement'));
const RolePermission = loadableComponent(() => import('@/pages/RolePermission'));
const TaskSchedule = loadableComponent(() => import('@/pages/TaskSchedule'));
const TaskCenter = loadableComponent(() => import('@/pages/TaskCenter'));
const SystemConfig = loadableComponent(() => import('@/pages/SystemConfig'));
const OperationLog = loadableComponent(() => import('@/pages/OperationLog'));
const SystemMonitor = loadableComponent(() => import('@/pages/SystemMonitor'));
const DataAccess = loadableComponent(() => import('@/pages/DataAccess'));
const ParserManagement = loadableComponent(() => import('@/pages/ParserManagement'));
const KnowledgeCompile = loadableComponent(() => import('@/pages/KnowledgeCompile'));
const KnowledgeCompileSearch = loadableComponent(() => import('@/pages/KnowledgeCompileSearch'));
const KnowledgeCompileGraph = loadableComponent(() => import('@/pages/KnowledgeCompileGraph'));
const KnowledgeCompileVector = loadableComponent(() => import('@/pages/KnowledgeCompileVector'));
const KnowledgeCompileCompile = loadableComponent(() => import('@/pages/KnowledgeCompileCompile'));
const DataOperations = loadableComponent(() => import('@/pages/DataOperations'));

export function getRoutes(mode: 'standalone' | 'hosted' = 'standalone', t?: (key: string) => string) {
  const inIframe = typeof window !== 'undefined' && window.parent !== window;
  const Layout = mode === 'hosted' || (isEmbedded() && inIframe) ? HostedLayout : BasicLayout;
  const T = t || ((s: string) => s);
  return [
    { path: '/', loader: () => redirect('/model-adapter') },
    {
      path: '',
      element: Layout,
      children: [
        {
          path: 'home',
          element: Home,
          title: T('platform.homeTitle'),
          menu: { icon: 'HomeOutlined', order: 1 },
        },
        {
          path: 'model-adapter',
          element: ModelAdapter,
          title: T('navigation.modelAdapter'),
          menu: { icon: 'ApiOutlined', order: 2, permissionCode: 'model:read' },
        },
        {
          path: 'tenant-management',
          element: TenantManagement,
          title: T('navigation.tenantManagement'),
          menu: { icon: 'TeamOutlined', order: 4, permissionCode: 'platform:tenant:read' },
        },
        {
          // 租户详情：无 menu 元数据 → 不进侧边栏/不进路由菜单精确匹配守卫；
          // 面包屑/文档标题走 title；详情数据当前由路由 state + 静态种子提供（待接后端详情接口）
          path: 'tenant-management/:id',
          element: TenantDetail,
          title: T('tenantManagement.detail.title'),
        },
        {
          path: 'user-management',
          element: UserManagement,
          title: T('navigation.userManagement'),
          menu: { icon: 'UserOutlined', order: 5, permissionCode: 'user:read' },
        },
        {
          path: 'role-permission',
          element: RolePermission,
          title: T('navigation.rolePermission'),
          menu: { icon: 'SafetyOutlined', order: 6, permissionCode: 'role:read' },
        },
        {
          path: 'task-schedule',
          element: TaskSchedule,
          title: T('navigation.taskSchedule'),
          menu: { icon: 'ScheduleOutlined', order: 7, permissionCode: 'platform:task:read' },
        },
        {
          // 任务中心：任务执行实例的列表（一条 = 一次文档入库的执行记录），
          // 与上面的「任务调度」（cron 调度计划配置）是两回事，故并存
          path: 'task-center',
          element: TaskCenter,
          title: T('navigation.taskCenter'),
          // 2026-10-08 放开入口：去掉 `hidden`（`aa33fe57` 加的），侧栏重新出现该项。
          // BasicLayout 按 `permissionCode` 过滤，所以只有持 `task_center:read` 的人看得见，
          // 与 shell 顶栏的 `canSeeTaskCenter` 同码。
          //
          // 这个码与「任务调度」的 `platform:task:read` 是**两个码**（2026-10-08 分开）：
          // 两个不同页面共用一个码就无法分别授权。分开的代价写在规格 §四.3 —— 只被授了
          // `platform:task:read` 的自定义角色会失去任务中心入口，需人工补授。
          //
          // 服务端防线已在能力侧补齐（`capabilities/task_center/capability.py` 的
          // `_ACTION_PERMISSIONS` 按 read / write 判权），所以路由守卫只是第一道；
          // 两壳的守卫本来也不等价（standalone 的 basename 带尾斜杠，拦不住）。
          menu: { icon: 'ScheduleOutlined', order: 8, permissionCode: 'task_center:read' },
        },
        {
          path: 'system-config',
          element: SystemConfig,
          title: T('navigation.systemConfig'),
          menu: { icon: 'SettingOutlined', order: 9, permissionCode: 'platform:config:read' },
        },
        {
          path: 'operation-log',
          element: OperationLog,
          title: T('navigation.operationLog'),
          // [jonex] 权限重构 B1：platform:audit:read → audit:read（tenant scope）。
          // 审计日志已放开给租户管理员（方案 §8 第 3 条）：接口改为双码放行、
          // DB 菜单树 id 15 的码也改成 audit:read。这里若不同步改，租户管理员会
          // 在 shell 导航看到「操作日志」入口，点进来被 router/index.tsx 的守卫
          // 直接 redirect('/error?page=403') —— 三处（接口/菜单/路由）必须同批改。
          // 平台管理员天然持全部 tenant 码，改后仍可见可进。
          menu: { icon: 'FileTextOutlined', order: 10, permissionCode: 'audit:read' },
        },
        {
          path: 'data-access',
          element: DataAccess,
          title: T('navigation.dataAccessMethods'),
          // [jonex] 权限重构 B1：engine:read → datasource:read，与 DB 菜单树 id 7 对齐。
          // 原先数据源管理与解析器管理共用 engine:read，两个不同页面一个码 → 无法分别授权。
          // 两者都是 platform scope（仅平台管理员），所以当下没有可见的行为变化，
          // 但码不一致会在「只授 datasource:read」时让页面 403，属埋雷。
          menu: { icon: 'CloudServerOutlined', order: 11, permissionCode: 'datasource:read' },
        },
        {
          path: 'parser-management',
          element: ParserManagement,
          title: T('navigation.parserManagement'),
          menu: { icon: 'CodeOutlined', order: 12, permissionCode: 'engine:read' },
        },
        {
          // [jonex] 全局系统监控 —— 占位页（研发中）。给 platform:monitor:read 一个真实落点，
          // 避免角色权限页出现「勾了也没有入口」的权限项。见 docs/permissions/PERMISSIONS_REDESIGN.md §11.2
          path: 'system-monitor',
          element: SystemMonitor,
          title: T('navigation.systemMonitor'),
          menu: { icon: 'DashboardOutlined', order: 13, permissionCode: 'platform:monitor:read' },
        },
        { path: 'knowledge-compile', element: KnowledgeCompile, title: T('navigation.knowledgeCompile') },
        { path: 'knowledge-compile/search', element: KnowledgeCompileSearch, title: T('navigation.compileSearch') },
        { path: 'knowledge-compile/graph', element: KnowledgeCompileGraph, title: T('navigation.compileGraph') },
        { path: 'knowledge-compile/vector', element: KnowledgeCompileVector, title: T('navigation.compileVector') },
        { path: 'knowledge-compile/compile', element: KnowledgeCompileCompile, title: T('navigation.compileCompile') },
        // 数据运营：一个页面、三个 tab（tab 落在 ?tab=）。
        // 权限码 analytics:read 是**租户级**（本租户的检索/资产统计，接口在无 domain_space_id
        // 时要求租户管理员），当前只控菜单与前端可见性，不是接口防线——见
        // deploy/postgres/update/036_data_operations_permission_and_menu.sql 的边界说明。
        // order 3 是刻意的：与 DB 菜单树同序（平台管理分组里排在「账号与权限」之前），
        // 其余平台管理项顺延一位。图标名同时注册在 router/menu.ts 的 IconMap（未注册会渲染成空图标）。
        {
          path: 'data-operations',
          element: DataOperations,
          title: T('navigation.dataOperations'),
          menu: { icon: 'BarChartOutlined', order: 3, permissionCode: 'analytics:read' },
        },
      ],
    },
    // 权限守卫 redirect 目标：?page=403 显示 403（顶层无布局壳，与 404 视觉统一）
    { path: 'error', element: ErrorPage, title: T('error.403') },
    { path: '*', element: NotFound, title: '404' },
  ];
}
export default getRoutes('standalone');
