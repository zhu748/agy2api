import {
  AlertTriangle,
  Bug,
  Download,
  FileText,
  Info,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  Trash2,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import Modal from "../components/Modal";
import Select from "../components/Select";
import api from "../api";

const LEVEL_META = {
  critical: { label: "CRITICAL", cls: "bg-red-600/20 text-red-400 border-red-600/40", icon: XCircle },
  error: { label: "ERROR", cls: "bg-red-600/20 text-red-400 border-red-600/40", icon: XCircle },
  warning: { label: "WARNING", cls: "bg-amber-500/20 text-amber-400 border-amber-500/40", icon: AlertTriangle },
  info: { label: "INFO", cls: "bg-blue-600/20 text-blue-400 border-blue-600/40", icon: Info },
  debug: { label: "DEBUG", cls: "bg-gray-600/20 text-gray-400 border-gray-600/40", icon: Bug },
};

export default function Logs() {
  const [items, setItems] = useState([]);
  const [maxSize, setMaxSize] = useState(3000);
  const [levelFilter, setLevelFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [loading, setLoading] = useState(true);
  const [clearing, setClearing] = useState(false);
  const [message, setMessage] = useState(null);
  const [expanded, setExpanded] = useState(() => new Set());

  const fetchLogs = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const res = await api.get("/logs/recent", { params: { limit: 1000, level: levelFilter } });
      setItems(res.data.items || []);
      setMaxSize(res.data.max_size || 3000);
    } catch (err) {
      if (!silent) setMessage({ type: "error", text: err.response?.data?.detail || "加载运行日志失败" });
    } finally {
      if (!silent) setLoading(false);
    }
  }, [levelFilter]);

  useEffect(() => {
    fetchLogs();
  }, [fetchLogs]);

  // 自动刷新（10 秒静默轮询，不打扰浏览位置）
  useEffect(() => {
    if (!autoRefresh) return undefined;
    const timer = setInterval(() => fetchLogs(true), 10000);
    return () => clearInterval(timer);
  }, [autoRefresh, fetchLogs]);

  // 关键字搜索在前端本地过滤（缓冲最多 3000 条，无性能压力）
  const visibleItems = search.trim()
    ? items.filter((item) =>
        `${item.message} ${item.source}`.toLowerCase().includes(search.trim().toLowerCase())
      )
    : items;

  const clearLogs = async () => {
    if (!window.confirm("确定要清空运行日志缓冲吗？此操作仅影响面板展示，不影响服务运行。")) return;
    setClearing(true);
    try {
      const res = await api.post("/logs/clear");
      setMessage({ type: "success", text: res.data.message || "已清空运行日志" });
      fetchLogs(true);
    } catch (err) {
      setMessage({ type: "error", text: err.response?.data?.detail || "清空失败" });
    } finally {
      setClearing(false);
    }
  };

  const downloadLogs = async () => {
    try {
      const res = await api.get("/logs/download", { responseType: "blob" });
      const url = URL.createObjectURL(res.data);
      const a = document.createElement("a");
      a.href = url;
      const d = new Date();
      const pad = (x) => String(x).padStart(2, "0");
      a.download = `agy2api-logs-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}.txt`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      setMessage({ type: "error", text: err.response?.data?.detail || "下载失败" });
    }
  };

  const toggleExpand = (key) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const errorCount = items.filter((i) => i.level === "error" || i.level === "critical").length;

  return (
    <div className="space-y-4">
      {/* 概览 */}
      <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
        <div className="card p-4 flex items-center gap-3">
          <div className="p-2.5 bg-purple-600/20 rounded-lg">
            <FileText size={20} className="text-purple-400" />
          </div>
          <div className="min-w-0">
            <div className="text-xl font-bold">{items.length}<span className="text-sm text-gray-500 font-normal"> / {maxSize}</span></div>
            <div className="text-xs text-gray-500">缓冲日志（条）</div>
          </div>
        </div>
        <div className="card p-4 flex items-center gap-3">
          <div className="p-2.5 bg-red-600/20 rounded-lg">
            <XCircle size={20} className="text-red-400" />
          </div>
          <div className="min-w-0">
            <div className={`text-xl font-bold ${errorCount > 0 ? "text-red-400" : ""}`}>{errorCount}</div>
            <div className="text-xs text-gray-500">错误 / 严重</div>
          </div>
        </div>
        <div className="card p-4 flex items-center gap-3 col-span-2 lg:col-span-1">
          <div className={`p-2.5 rounded-lg ${autoRefresh ? "bg-green-600/20" : "bg-gray-600/20"}`}>
            {autoRefresh ? <Play size={20} className="text-green-400" /> : <Pause size={20} className="text-gray-400" />}
          </div>
          <div className="min-w-0">
            <div className="text-sm font-medium">{autoRefresh ? "自动刷新中" : "自动刷新已暂停"}</div>
            <div className="text-xs text-gray-500">每 10 秒静默拉取</div>
          </div>
        </div>
      </div>

      {/* 操作栏 */}
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={() => fetchLogs()} disabled={loading} className="btn btn-secondary text-xs sm:text-sm px-3 py-1.5 sm:px-4 sm:py-2 flex items-center gap-1.5">
          <RefreshCw size={15} className={loading ? "animate-spin" : ""} />
          刷新
        </button>
        <button onClick={() => setAutoRefresh((v) => !v)} className={`btn text-xs sm:text-sm px-3 py-1.5 sm:px-4 sm:py-2 flex items-center gap-1.5 ${autoRefresh ? "btn-secondary" : "btn-danger"}`}>
          {autoRefresh ? <Pause size={15} /> : <Play size={15} />}
          {autoRefresh ? "暂停刷新" : "开启刷新"}
        </button>
        <button onClick={downloadLogs} className="btn btn-secondary text-xs sm:text-sm px-3 py-1.5 sm:px-4 sm:py-2 flex items-center gap-1.5">
          <Download size={15} />
          下载日志
        </button>
        <button onClick={clearLogs} disabled={clearing} className="btn btn-danger text-xs sm:text-sm px-3 py-1.5 sm:px-4 sm:py-2 flex items-center gap-1.5">
          {clearing ? <Loader2 size={15} className="animate-spin" /> : <Trash2 size={15} />}
          清空
        </button>
      </div>

      {/* 消息弹窗 */}
      {message && (
        <Modal type={message.type} title={message.type === "success" ? "操作成功" : "操作失败"} onClose={() => setMessage(null)}>
          <span className="whitespace-pre-wrap break-all">{message.text}</span>
        </Modal>
      )}

      {/* 筛选器 */}
      <div className="grid grid-cols-2 sm:flex sm:flex-wrap gap-2 sm:gap-3 text-sm">
        <Select
          value={levelFilter}
          onChange={(v) => setLevelFilter(v)}
          className="w-full sm:w-40"
          options={[
            { value: "all", label: "级别: 全部" },
            { value: "info", label: "INFO 及以上" },
            { value: "warning", label: "WARNING 及以上" },
            { value: "error", label: "仅 ERROR / CRITICAL" },
          ]}
        />
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="关键字搜索（如：写入凭证失败）"
          className="w-full sm:flex-1 bg-dark-800 border border-dark-600 rounded-lg px-3 py-2 text-gray-200 text-xs sm:text-sm placeholder-gray-500"
        />
      </div>

      {/* 日志列表（新→旧；长日志默认折叠，点击展开） */}
      {loading && items.length === 0 ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="animate-spin text-purple-500" size={28} />
        </div>
      ) : visibleItems.length === 0 ? (
        <div className="card p-8 text-center text-gray-500">
          {search.trim() ? "没有匹配关键字的日志" : "暂无运行日志"}
        </div>
      ) : (
        <div className="space-y-2">
          {visibleItems.map((item, index) => {
            const meta = LEVEL_META[item.level] || LEVEL_META.info;
            const LevelIcon = meta.icon;
            const key = `${item.ts}-${index}`;
            const isOpen = expanded.has(key);
            return (
              <div
                key={key}
                onClick={() => toggleExpand(key)}
                className={`card p-3 sm:p-4 cursor-pointer hover:border-purple-500/40 transition-colors ${isOpen ? "" : "overflow-hidden"}`}
              >
                <div className="flex items-center gap-2 flex-wrap text-xs">
                  <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border font-semibold ${meta.cls}`}>
                    <LevelIcon size={11} />
                    {meta.label}
                  </span>
                  <span className="text-gray-500 font-mono">{item.ts}</span>
                  <span className="text-gray-600 font-mono truncate max-w-[40%]">[{item.source}]</span>
                </div>
                <pre
                  className={`mt-2 font-mono text-xs sm:text-[13px] leading-relaxed text-gray-300 whitespace-pre-wrap break-all ${
                    isOpen ? "" : "max-h-12"
                  }`}
                >
                  {item.message}
                </pre>
                {!isOpen && item.message.split("\n").length > 2 && (
                  <div className="mt-1 text-[11px] text-gray-600">…点击展开全部 {item.message.split("\n").length} 行</div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <p className="text-xs text-gray-600 leading-relaxed">
        运行日志保存在内存环形缓冲中（最近 {maxSize} 条，服务重启后清空），凭证写入失败、OAuth
        异常等错误都会在这里出现。若部署了多个 Worker，此处仅显示当前进程的日志。
      </p>
    </div>
  );
}
