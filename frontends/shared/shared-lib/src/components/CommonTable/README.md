# CommonTable

把「筛选区 + 取数 + 表格 + 三态 + 分页」收敛成一个组件，供新增列表页使用。

归属 `@jonex/shared-lib`，各子应用均可引入。组件内的文案走 i18next 的 `common.*` key（与 `PageHeader` 同一约定），由消费方的语言包提供。

## 适用与不适用

**适用：** 顶部筛选区（搜索框 / 下拉）+ antd Table + 分页器的标准列表页；数据由服务端分页返回。带左侧目录树、行选择、行拖拽的页面同样适用——外围布局由页面自己组织，表格自身的能力走下方的具名 props。

**不适用：** Card Grid 这类根本不是表格的形态；以及「一次拉全量 + 前端切片」的分页（组件只做服务端分页）。

**边界口径：** 表格能力一律以**具名 prop** 补齐（见「表格自身的额外能力」），不开 `tableProps` 之类的通用透传。这样每加一项能力都要显式过一遍，API 不会退化成「把所有页面逻辑重新排列一遍」。

## 最小用法

```tsx
import { CommonTable } from '@jonex/shared-lib';

<CommonTable<User, { keyword: string }>
  filters={[{ key: 'keyword', label: '关键词', type: 'input', placeholder: '搜索用户' }]}
  rowKey="id"
  columns={columns}
  fetch={async ({ page, pageSize, keyword }) => {
    const res = await getUsers({ page, pageSize, keyword });
    return { list: res.items, total: res.total };
  }}
/>
```

`fetch` **始终**收到 `{ page, pageSize, ...筛选值 }` 的单一对象，签名不随分页模式变化。`filters` 里 `key` 就是筛选值在 `fetch` 参数里的字段名。

## 筛选区

筛选区是一个 antd `Form`（`layout="horizontal"`）+ `Row` / `Col` 栅格，**默认一行 4 个**，用 `filterColsPerRow` 调整：

```tsx
<CommonTable
  filterColsPerRow={3}
  filters={[
    { key: 'keyword', label: '关键词', type: 'input' },
    {
      key: 'phase',
      label: '文档状态',
      type: 'select',
      mode: 'multiple',
      options: [{ label: '待解析', value: 'pending_parse' }],
    },
  ]}
  /* ... */
/>
```

`label` 必填，传**已翻译好的文本**——组件不替页面做 i18n，与 `placeholder` 一致。控件默认铺满所在列，`width` 只在需要固定宽度时才传。

**label 与控件同行**，每个筛选项占一个等宽栅格（`Span = 24 / filterColsPerRow`）。label 定宽 `5.5em` 并右对齐——这一条是**等宽的前提**：label 若按内容自适应，「关键词」比「文档状态」少一个字，同一行里两个控件的宽度就对不齐了。更长的 label（英文）会折行，但不撑破栅格。

**查询是即时的**：`input` 防抖（默认 300ms）后自动重查，`select` 选完即查，没有「查询」按钮——与其余列表页的做法一致。

## 按钮区

筛选区右侧固定渲染「刷新」与「重置」，与筛选项同处一个 `Row`，垂直居中对齐控件行：

| 按钮 | 行为 |
|---|---|
| 刷新 | 用当前筛选条件与页码重新取数（等价于 `ref.refresh()`） |
| 重置 | 清空**全部**筛选条件并回到第 1 页。`filterExtra` 写进上下文的 key 一并清掉 |

```tsx
<CommonTable
  actions={{ reset: false }}  // 只留「刷新」
  toolbar={<Button type="primary">新增</Button>}  // 自定义按钮排在默认按钮左侧
  /* ... */
/>
```

- `actions.refresh` / `actions.reset` 传 `false` 可分别关掉，默认都显示。
- 页面**没有任何**筛选控件时，「重置」不渲染——它无事可做。
- `toolbar` 是额外的按钮插槽（新增 / 批量导出等），排在「刷新」「重置」左边；`重置` 在最右，与常见后台「查询 / 重置」的收尾一致。

重置会重新挂载筛选项（`filters` 与 `filterExtra` 的控件，靠 `resetVersion` 换 key），因此连「刚敲完还没到防抖时间」的输入也一并清掉，不会在 300ms 后被写回来。

**`filterExtraRow` 不参与这次重挂载**：它的容器不随 `resetVersion` 变化，重置对它只做一件事——把 `ctx` 里的值清空。所以放在 `filterExtraRow` 里的控件必须写成**受控组件**（`value={ctx.get('status')}`），复位全靠自己从 `ctx` 读到空值；内部持有非受控状态的控件（`defaultValue`、自己 `useState` 的输入框）重置后会停在旧值上，与界面其余部分脱节。

## 整行的筛选扩展位

需要整行宽度、又**参与筛选**的控件（典型是「全部 / 处理中 / 已完成 …」这类带计数的状态标签栏）走 `filterExtraRow`。它渲染在筛选行**之下**、表格之上，拿到的 `FilterContext` 与 `filterExtra` 完全相同：

```tsx
<CommonTable
  filters={[{ key: 'keyword', label: '关键词', type: 'input' }]}
  filterExtraRow={(ctx) => (
    <Segmented
      value={ctx.get('status') ?? ''}
      onChange={(v) => ctx.set('status', v as string)}
      options={[
        { label: '全部', value: '' },
        { label: '处理中', value: 'running' },
      ]}
    />
  )}
  /* ... */
/>
```

`ctx.set` 写入的值和配置项走同一条路径，因此「重置」会把它一并清掉、页码同时回到第 1 页——这正是它不能用 `toolbar` 替代的原因（`toolbar` 是 `ReactNode`，拿不到上下文，状态只能另起一份页面 state，重置后会与界面脱节）。

## 分页

| `pagination` | 行为 |
|---|---|
| `'server'`（默认） | 每次筛选变化或翻页各请求一次，分页器接 `total` |
| `false` | 只在筛选变化时请求一次，`page` 恒为 1，表格不显示分页器 |

## 表格自身的额外能力

组件自持 `columns` / `rowKey` / `dataSource` / `loading` / `pagination` / `locale`，其余表格能力以具名 prop 提供：

| prop | 说明 |
|---|---|
| `className` | 挂到组件**根容器**上。列表页要铺满宿主并在内部滚动时，靠它把高度链路接上 |
| `expandable` | 行展开，如父任务行下渲染子阶段 |
| `rowSelection` | 行选择。页面自己持有 `selectedRowKeys` |
| `onRow` | 行级 DOM 事件，主要用于 HTML5 拖拽 |
| `rowClassName` | 行 class，如拖拽中高亮 |
| `scroll` | 固定表头 / 横向滚动。`y` 只吃具体像素 |
| `size` | antd 默认 `large`，列表页一般传 `middle` |

```tsx
<CommonTable
  className="doc-library-table"
  rowSelection={{ type: 'checkbox', selectedRowKeys, onChange: setSelectedRowKeys }}
  onRow={(record) => ({ draggable: true, onDragStart: handleDragStart(record) })}
  rowClassName={(record) => (draggingId === record.id ? 'is-dragging' : '')}
  scroll={{ x: 1100, y: bodyHeight }}
  size="middle"
  onQueryChange={() => setSelectedRowKeys([])}
  /* ... */
/>
```

**`className` 挂在根容器上**，不是 antd Table 上。页面需要给表格挂 CSS 时应从根容器往下写，例如：

```scss
.doc-library-table {
  height: 100%;

  > .ant-table-wrapper { flex: 1; min-height: 0; }   // 根是 flex column，Table 是它后面的那个子项
  > .ant-table-wrapper .ant-table-body { flex: 1; overflow-y: auto !important; }
  > .ant-table-wrapper .ant-pagination { flex-shrink: 0; }
}
```

`scroll.y` 的取值由页面自己量（常见做法是给宿主容器挂 `ResizeObserver`）——组件不知道自己在多大的盒子里。

### `onQueryChange` 与「清空行选中」

`onQueryChange` 在**查询条件**（页码 / 每页条数 / 筛选值）变化时回调，首次取数也会触发一次；`ref.refresh()` 只是重取同一份数据、条件没变，**不触发**——否则点一次「刷新」就把用户勾选的行清空了。

典型用法是清空行选中：翻页或改筛选后，旧选中可能已不在当前结果集里，批量操作会打错对象。

```tsx
const handleQueryChange = useCallback(() => {
  // 返回同一引用让 React 跳过无谓的重渲染
  setSelectedRowKeys((prev) => (prev.length ? [] : prev));
}, []);
```

页面**自己管理**的查询条件（如左侧目录树选中的文件夹）不在它的观测范围内，需要页面自己清一次选中，并用 `ref.refresh({ resetPage: true })` 触发重取——见下。

### 页面自己管理的查询条件

有些查询条件不在筛选区里（典型是左侧目录树选中的文件夹）。这类条件写进 `fetch` 的闭包里即可，但它变化时组件收不到通知，需要页面显式说一声：

```tsx
// 首次挂载跳过——组件自己会取一次，不必再补一刀
const folderEffectMounted = useRef(false);
useEffect(() => {
  if (!folderEffectMounted.current) {
    folderEffectMounted.current = true;
    return;
  }
  setSelectedRowKeys([]);
  tableRef.current?.refresh({ resetPage: true });
}, [selectedKeys]);
```

`resetPage` 不能省：页面本来就停在第 1 页时，光重置页码不产生状态变化，也就不会重取——`refresh()` 内部的 tick 才是那次请求的可靠触发点。

## 自定义筛选控件

配置项表达不了的控件（如日期区间、级联选择）走 `filterExtra`。它拿到的是组件内部的筛选值句柄，`ctx.set` 与配置项写的是同一份值、走同一套「重置页码」逻辑：

```tsx
<CommonTable
  filters={[{ key: 'keyword', label: '关键词', type: 'input' }]}
  filterExtra={(ctx) => (
    // filterExtra 已经落在筛选区栅格内；需要 label 时自己包一层 Form.Item，
    // 并照抄筛选区那套 labelCol / wrapperCol，宽度才和配置项对齐
    <Form.Item label="上传时间" labelCol={{ flex: '0 0 5.5em', style: { textAlign: 'right' } }}
      wrapperCol={{ flex: '1 1 0%', style: { minWidth: 0 } }} style={{ marginBottom: 0 }}>
      <DatePicker.RangePicker
        value={ctx.get('uploadedAt')}
        onChange={(v) => ctx.set('uploadedAt', v)}
      />
    </Form.Item>
  )}
  /* ... */
/>
```

> 优先用 `filters` 配置项。凡是「一个 key + 一个 antd 表单控件」的筛选项都能配出来（含 `select` 多选），`filterExtra` 留给真正需要组合多个控件的场景。

注意 `ctx.values` 里是**已生效**的值。`input` 类型在用户输入过程中不会实时写入——防抖落地后才写。

## 常用 props

| prop | 说明 |
|---|---|
| `filters` | 筛选控件声明数组，每项需带 `key` 与 `label`；支持 `input`（含 `debounce`，默认 300ms）与 `select`（含 `mode: 'multiple'`、`maxTagCount`） |
| `filterColsPerRow` | 筛选区每行几个筛选项，默认 4 |
| `filterExtra` | `(ctx) => ReactNode`，自定义筛选控件 |
| `toolbar` | 额外的操作按钮插槽，如「新增」 |
| `filterExtraRow` | `(ctx) => ReactNode`，筛选行之下独占一行的扩展位，用于参与筛选的控件组 |
| `expandable` | 透传给 antd Table 的行展开配置 |
| `actions` | `{ refresh?: boolean; reset?: boolean }`，默认两个按钮都显示 |
| `columns` / `rowKey` / `emptyText` | 透传给 antd Table |
| `className` / `rowSelection` / `onRow` / `rowClassName` / `scroll` / `size` | 表格自身的额外能力，见「表格自身的额外能力」 |
| `onQueryChange` | 查询条件变化时回调，页面用它清空行选中 |
| `onError` | `fetch` 失败时回调，页面用它决定要不要 `message.error`；组件本身不弹 toast |
| `defaultPageSize` | 默认 10 |
| `ref.refresh({ resetPage? })` | 用当前筛选条件与页码重新拉取，适合增删改后调用；`resetPage: true` 时先回到第 1 页，给页面自己管理的查询条件用 |
| `ref.reset()` | 清空全部筛选条件并回到第 1 页，与内置「重置」按钮同一个动作。给 `actions.reset=false`、页面自己摆按钮的场景用 |

## 三态

- **loading**：透传 antd Table 的 `loading`。
- **空态**：`emptyText` 优先；不传则用 `common.noData`。
- **错误态**：清空列表，表格照常渲染（表头还在、没有数据行），**组件不渲染任何兜底**——失败提示由页面在 `onError` 里自己发。
  清空是必须的：残留的旧数据会被误读成「筛选后的最新结果」。不渲染兜底也是刻意的：接不上后端与「确实没有数据」在表格里长得一样，但只有页面知道该说什么。
  所以**接了 `fetch` 就要接 `onError`**，否则失败会静默成一次空列表。
