/* 稳稿工作台（RS-9）核心：状态、API、SSE、引导。
   数据全部来自后端；前端不编造任何步骤、来源或检查结果。 */
(function () {
  "use strict";

  var WB = window.WB = {
    state: {
      jobs: [],
      currentJobId: null,
      job: null,          // /api/jobs/<id> 落盘状态
      events: [],         // C2 事件流
      research: null,
      images: null,
      checks: null,
      decisions: null,
      localrun: null,
      meta: null,
      objects: null,
      theme: "ink",
      pages: [],          // [{page_no, url}]
      currentPage: 1,
      tab: "preview",
      filter: "all",
      running: false,
      terminal: null,     // 终态：DELIVERED / FAILED / CANCELLED / READY ...
      sse: null,
      lang: "auto",
      researchOn: true,
    },
  };

  // ------------------------------------------------------------ API
  WB.api = function (path, opts) {
    opts = opts || {};
    var init = {
      method: opts.method || "GET",
      headers: { "Accept": "application/json" },
    };
    if (opts.body) {
      init.method = "POST";
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (r) {
      var ct = r.headers.get("Content-Type") || "";
      if (ct.indexOf("application/json") >= 0) return r.json();
      return r.text();
    }).then(function (d) {
      if (d && d.error) throw new Error(d.error.message || "请求失败");
      return d;
    });
  };

  WB.toast = function (msg) {
    var t = document.getElementById("toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(WB._toastTimer);
    WB._toastTimer = setTimeout(function () { t.hidden = true; }, 3200);
  };

  WB.esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };

  // ------------------------------------------------------------ 引导
  WB.boot = function () {
    WB.api("/api/meta").then(function (m) { WB.state.meta = m; }).catch(function () {});
    WB.loadLocalrun();
    WB.loadOverview();
    WB.bindComposer();
    WB.bindTabs();
    WB.bindGlobalButtons();
    setInterval(WB.loadOverview, 15000);
  };

  WB.loadLocalrun = function () {
    WB.api("/api/localrun").then(function (d) {
      WB.state.localrun = d;
      WB.renderLocalrun();
    }).catch(function () {
      document.getElementById("localrun-rows").innerHTML =
        '<div class="hint">本机环境探测失败。</div>';
    });
  };

  WB.loadOverview = function () {
    WB.api("/api/overview").then(function (d) {
      WB.state.jobs = d.jobs || [];
      WB.renderJobList();
    }).catch(function () { /* 服务未就绪时保持空态 */ });
  };

  // ------------------------------------------------------------ 选中任务
  WB.selectJob = function (jobId) {
    if (WB.state.currentJobId === jobId) return;
    WB.state.currentJobId = jobId;
    WB.state.currentPage = 1;
    WB.state.events = [];
    WB.state.terminal = null;
    WB.closeSSE();
    WB.api("/api/jobs/" + encodeURIComponent(jobId)).then(function (job) {
      WB.state.job = job;
      WB.state.terminal = _terminal_of(job);
      WB.renderJobHead();
      WB.openSSE(jobId);
    }).catch(function () {
      // 新管线任务没有 runner 的 job.json：回退到管线状态端点
      WB.api("/api/jobs/" + encodeURIComponent(jobId) + "/pipeline-state").then(function (job) {
        WB.state.job = job;
        WB.state.terminal = job.status;
        WB.renderJobHead();
        WB.openSSE(jobId);
      }).catch(function () { WB.state.job = null; });
    });
    WB.loadEvidence(jobId);
    WB.renderJobList();
  };

  function _terminal_of(job) {
    var v = (job && job.validation && job.validation.verdict) ||
            (job && job.runner && job.runner.verdict);
    if (!v) return null;
    if (v === "READY" || v === "READY_WITH_EXCEPTIONS" || v === "FAILED" ||
        v === "NEEDS_REVIEW" || v === "CANCELLED") return v;
    return null;  // QUEUED/RUNNING/VALIDATING 仍在进行
  }

  WB.loadEvidence = function (jobId) {
    WB.api("/api/jobs/" + jobId + "/research").then(function (d) {
      WB.state.research = d; WB.renderSources();
    }).catch(function () {});
    WB.api("/api/jobs/" + jobId + "/images").then(function (d) {
      WB.state.images = d; WB.renderImages();
    }).catch(function () {});
    WB.api("/api/jobs/" + jobId + "/checks").then(function (d) {
      WB.state.checks = d; WB.renderPageCheck(); WB.renderChatExtras();
    }).catch(function () {});
    WB.api("/api/jobs/" + jobId + "/decisions").then(function (d) {
      WB.state.decisions = d; WB.renderChatExtras(); WB.renderSources();
    }).catch(function () {});
    WB.loadPageObjects(jobId);
    WB.loadPages(jobId);
  };

  WB.loadPageObjects = function (jobId) {
    WB.api("/api/jobs/" + encodeURIComponent(jobId) + "/objects").then(function (d) {
      WB.state.objects = d;
      WB.renderPageObjects();
    }).catch(function () {
      WB.state.objects = null;
      WB.renderPageObjects();
    });
  };

  WB.loadPages = function (jobId) {
    // 页数：先问旧接口（有候选渲染缓存时最可靠），再按事件/计划推断
    WB.api("/api/jobs/" + jobId + "/pages?side=candidate").then(function (d) {
      var pages = (d.pages || []).map(function (p) {
        return { page_no: p.page_no, url: "/api/jobs/" + jobId + "/page-image/" + p.page_no + ".png" };
      });
      if (pages.length) { WB.state.pages = pages; WB.renderPreview(); return; }
      WB.guessPages(jobId);
    }).catch(function () { WB.guessPages(jobId); });
  };

  WB.guessPages = function (jobId) {
    // 新管线候选：从 1 开始探测 page-N.png，最多 60 页
    var pages = [];
    var probe = function (n) {
      if (n > 60) { WB.state.pages = pages; WB.renderPreview(); return; }
      var url = "/api/jobs/" + jobId + "/page-image/" + n + ".png";
      fetch(url, { method: "HEAD" }).then(function (r) {
        if (r.ok) { pages.push({ page_no: n, url: url }); }
        if (n === 1 && !r.ok) { WB.state.pages = []; WB.renderPreview(); return; }
        probe(n + 1);
      }).catch(function () { WB.state.pages = pages; WB.renderPreview(); });
    };
    probe(1);
  };

  // ------------------------------------------------------------ SSE
  WB.openSSE = function (jobId) {
    try {
      var es = new EventSource("/api/jobs/" + encodeURIComponent(jobId) + "/events?follow=1");
      WB.state.sse = es;
      WB.state.running = true;
      es.onmessage = function (e) {
        try {
          var ev = JSON.parse(e.data);
          WB.state.events.push(ev);
          WB.renderTimeline();
          WB.renderJobHead();
        } catch (err) { /* 心跳或坏帧忽略 */ }
      };
      es.onerror = function () { WB.closeSSE(); };
      // 终态任务：事件回放完后关闭连接，不无限长挂
      if (WB.state.terminal) {
        setTimeout(function () {
          if (WB.state.terminal) WB.closeSSE();
        }, 2500);
      }
    } catch (err) { /* 不支持 SSE 时退回轮询 */ }
  };

  WB.closeSSE = function () {
    if (WB.state.sse) { try { WB.state.sse.close(); } catch (e) {} WB.state.sse = null; }
    WB.state.running = false;
  };

  document.addEventListener("DOMContentLoaded", WB.boot);
})();
