<template>
  <section class="page" data-module="session">
    <header class="page-head">
      <div>
        <h2>采集会话管理</h2>
        <p class="page-desc">按目标站点维护采集会话：凭证到期前按刷新策略验证，联动反爬策略与代理，验证失败生成可追踪告警。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记采集会话</button>
        <button class="btn" type="button" @click="runSweep">到期扫描</button>
        <button class="btn" type="button" @click="exportRows">导出采集会话清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="runValidate(row)">验证凭证</button>
            <button class="link" type="button" @click="showEffective(row)">生效配置</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集会话数据，可先登记采集会话</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集会话记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="noticeMessage">{{ noticeMessage }}</span>
    </footer>

    <h3 class="section-title">验证流水（追加式，不覆盖历史）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in historyColumns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in historyRows" :key="String(row.id)">
          <td v-for="column in historyColumns" :key="column">{{ row[column] ?? '—' }}</td>
        </tr>
        <tr v-if="!historyRows.length">
          <td :colspan="historyColumns.length" class="empty-state">暂无验证流水</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/session'
const columns = ["会话编号", "目标站点", "凭证编号", "反爬策略", "绑定代理", "生效UA", "生效Referer", "生效延迟", "最近验证时间", "追踪编号"]
const historyColumns = ["追踪编号", "会话编号", "凭证编号", "触发方式", "验证结果", "绑定代理", "详情", "记录时间"]
const statuses = ["待验证", "有效", "已过期", "已阻断"]

const rows = ref<Row[]>([])
const historyRows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 2)
const stats = ref([
  { label: '会话总数', value: 0 },
  { label: '有效会话', value: 0 },
  { label: '已阻断会话', value: 0 },
  { label: '待处理告警', value: 0 },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '采集会话登记入口尚未接入审批流'
}

async function runValidate(row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/validate`, { method: 'POST' })
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message ?? '验证未通过'
    } else {
      noticeMessage.value = payload.message ?? '验证成功'
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集会话验证失败'
  }
}

async function showEffective(row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/effective?site=${encodeURIComponent(String(row['目标站点'] ?? ''))}`)
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.detail ?? '生效配置读取失败')
    }
    const conflicts = (payload['冲突'] as string[]).length ? `；冲突：${(payload['冲突'] as string[]).join('、')}` : ''
    noticeMessage.value = `生效配置来源：${payload['判定来源']}，代理 ${payload['绑定代理']}，UA ${payload['生效UA']}，Referer ${payload['生效Referer']}，延迟 ${payload['生效延迟']}${conflicts}`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生效配置读取失败'
  }
}

async function runSweep() {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/sweep`, { method: 'POST' })
    const payload = await response.json()
    noticeMessage.value = payload.message ?? '扫描完成'
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '到期扫描失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [listResp, historyResp, statsResp] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/history`),
      request(`${ENDPOINT}/stats`),
    ])
    if (!listResp.ok) {
      throw new Error('采集会话列表读取失败')
    }
    const payload = await listResp.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (historyResp.ok) {
      const historyPayload = await historyResp.json()
      historyRows.value = historyPayload.items ?? []
    }
    if (statsResp.ok) {
      const statsPayload = await statsResp.json()
      stats.value = stats.value.map((item) => ({ ...item, value: statsPayload[item.label] ?? 0 }))
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集会话列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.section-title {
  margin: 24px 0 12px;
  font-size: 15px;
}
</style>
