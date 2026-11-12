<template>
  <section class="page" data-module="rpa">
    <header class="page-head">
      <div>
        <h2>RPA流程配置管理</h2>
        <p class="page-desc">维护RPA自动化流程的动作编排、等待条件与执行结果，实现无人值守数据采集。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记RPA流程</button>
        <button class="btn" type="button" @click="exportRows">导出RPA流程配置清单</button>
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
          <th>状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="startTrial(row)">试运行</button>
            <button class="link" type="button" @click="formalExecute(row)">正式执行</button>
            <button class="link" type="button" @click="loadRuns(row)">运行记录</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无RPA流程配置数据，可先登记RPA流程</td>
        </tr>
      </tbody>
    </table>

    <section v-if="selectedFlow" class="runs-panel">
      <header class="runs-head">
        <strong>{{ selectedFlow['流程编号'] }} 的试运行 / 正式执行记录</strong>
        <button class="btn ghost" type="button" @click="closeRuns">收起</button>
      </header>
      <p v-if="!runs.length" class="empty-state">暂无运行记录，可先发起试运行</p>
      <article v-for="run in runs" :key="run['运行编号']" class="run-card">
        <header class="run-head">
          <span>
            {{ run['运行编号'] }} · {{ run['类型'] }} · {{ run['状态'] }}
            <template v-if="run['类型'] === '试运行' && run['状态'] === '超时待重试'">
              （停在第 {{ run['当前步骤'] }} 步）
            </template>
          </span>
          <span class="run-meta">
            {{ run['条件来源'] }} · 版本 v{{ run['版本号'] }} · 重试 {{ run['重试次数'] }} 次 · {{ run['开始时间'] }}
          </span>
          <button
            v-if="run['类型'] === '试运行' && run['状态'] === '超时待重试'"
            class="link"
            type="button"
            @click="retryRun(run)"
          >
            从第 {{ run['当前步骤'] }} 步重试
          </button>
        </header>
        <table class="data-table step-table">
          <thead>
            <tr>
              <th>步骤</th>
              <th>动作</th>
              <th>等待条件</th>
              <th>超时秒数</th>
              <th>结果字段</th>
              <th>运行环境</th>
              <th>结果</th>
              <th>等待秒数</th>
              <th>证据</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(step, index) in run['步骤结果']" :key="index">
              <td>{{ step['步骤号'] }}</td>
              <td>{{ step['动作'] }}</td>
              <td>{{ step['等待条件'] }}</td>
              <td>{{ step['超时秒数'] }}</td>
              <td>{{ step['结果字段'] }}</td>
              <td>{{ step['运行环境'] }}</td>
              <td :class="step['结果'] === '超时' ? 'error-text' : 'ok-text'">{{ step['结果'] }}</td>
              <td>{{ step['等待秒数'] }}</td>
              <td class="evidence">{{ step['证据'] }}</td>
            </tr>
          </tbody>
        </table>
      </article>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条RPA流程配置记录</span>
      <span v-if="noticeMessage" class="ok-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/rpa'
const columns = ["流程编号", "流程名称", "输入来源", "动作序列", "等待条件", "超时秒数", "结果字段", "运行环境"]
const actions = ["提交校验", "启用配置", "停用配置"]
const stats = [{"label": "RPA流程总数", "value": 0}, {"label": "待校验RPA流程", "value": 0}, {"label": "已启用RPA流程", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const selectedFlow = ref<Row | null>(null)
const runs = ref<Row[]>([])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = 'RPA流程登记入口尚未接入审批流'
}

function closeRuns() {
  selectedFlow.value = null
  runs.value = []
}

async function postAction(path: string, values: Record<string, any>): Promise<Row | null> {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(path, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || 'RPA流程配置动作未生效，请稍后重试')
    }
    noticeMessage.value = payload.message || '操作已完成'
    return payload.entry ?? null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'RPA流程配置操作失败'
    return null
  }
}

async function runAction(action: string, row: Row) {
  const entry = await postAction(`${ENDPOINT}/${row.id}/actions`, { action })
  if (entry) {
    await reload()
  }
}

async function startTrial(row: Row) {
  await postAction(`${ENDPOINT}/${row.id}/trial`, {})
  await loadRuns(row)
}

async function formalExecute(row: Row) {
  await postAction(`${ENDPOINT}/${row.id}/execute`, {})
  await loadRuns(row)
}

async function retryRun(run: Row) {
  await postAction(`${ENDPOINT}/${run['流程id']}/runs/${run.id}/retry`, {})
  if (selectedFlow.value) {
    await loadRuns(selectedFlow.value)
  }
}

async function loadRuns(row: Row) {
  selectedFlow.value = row
  try {
    const response = await request(`${ENDPOINT}/${row.id}/runs`)
    if (!response.ok) {
      throw new Error('运行记录读取失败')
    }
    const payload = await response.json()
    runs.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行记录读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('RPA流程列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'RPA流程配置列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.runs-panel {
  margin-top: 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
}
.runs-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.run-card {
  border: 1px solid var(--border);
  border-radius: 6px;
  margin-bottom: 10px;
  padding: 8px 10px;
}
.run-head {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 6px;
  font-size: 13px;
}
.run-meta {
  color: var(--muted);
  font-size: 12px;
}
.step-table th,
.step-table td {
  font-size: 12px;
}
.evidence {
  max-width: 360px;
  white-space: normal;
  color: var(--muted);
}
.ok-text {
  color: #067647;
}
</style>
