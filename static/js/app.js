/* 公共工具：请求封装、导航渲染、转义、提示等 */
(function () {
  const NAV = [
    { href: "/", label: "首页", key: "home" },
    { href: "/jobs", label: "求职信息", key: "jobs" },
    { href: "/interns", label: "实习专区", key: "interns" },
    { href: "/plan", label: "职业规划", key: "plan" },
    { href: "/resume", label: "简历制作", key: "resume" },
    { href: "/toolbox", label: "求职工具箱", key: "toolbox" },
    { href: "/roadmap", label: "迭代记录", key: "roadmap" },
    { href: "/about", label: "关于项目", key: "about" },
  ];

  function esc(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function isAuthPage() {
    const p = location.pathname;
    return p.startsWith("/login") || p.startsWith("/register");
  }

  async function request(path, options) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options || {}));
    let payload = null;
    try { payload = await res.json(); } catch (e) { payload = null; }
    if (res.status === 401 && !(options && options.noRedirect) && !isAuthPage()) {
      location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
      throw new Error("登录已失效，正在跳转登录页");
    }
    if (!res.ok || !payload || payload.code !== 0) {
      throw new Error((payload && payload.msg) || "请求失败（" + res.status + "）");
    }
    return payload.data;
  }

  const api = {
    stats: () => request("/api/stats"),
    filters: () => request("/api/filters"),
    jobs: (params) => request("/api/jobs?" + new URLSearchParams(params).toString()),
    job: (id) => request("/api/jobs/" + id),
    companies: (params) => request("/api/companies?" + new URLSearchParams(params || {}).toString()),
    company: (id) => request("/api/companies/" + id),
    quiz: () => request("/api/quiz"),
    submitQuiz: (answers) => request("/api/quiz/submit", { method: "POST", body: JSON.stringify({ answers }) }),
    interview: (params) => request("/api/interview-questions?" + new URLSearchParams(params || {}).toString()),
    salary: (params) => request("/api/salary-stats?" + new URLSearchParams(params || {}).toString()),
    applications: (phone) => request("/api/applications?phone=" + encodeURIComponent(phone)),
    apply: (body) => request("/api/applications", { method: "POST", body: JSON.stringify(body) }),
    me: () => request("/api/auth/me"),
    login: (username, password) =>
      request("/api/auth/login", { method: "POST", noRedirect: true, body: JSON.stringify({ username, password }) }),
    register: (username, password, displayName) =>
      request("/api/auth/register", { method: "POST", noRedirect: true,
        body: JSON.stringify({ username, password, display_name: displayName }) }),
    logout: () => request("/api/auth/logout", { method: "POST" }),
  };

  function toast(msg, type) {
    let wrap = document.querySelector(".toast-wrap");
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.className = "toast-wrap";
      document.body.appendChild(wrap);
    }
    const el = document.createElement("div");
    el.className = "toast " + (type || "");
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(() => el.remove(), 2800);
  }

  function salaryText(min, max, unit) {
    if (!min && !max) return "面议";
    if (unit === "日薪" || (!unit && min > 0 && min < 1000)) return min + "-" + max + "元/天";
    return Math.round(min / 1000) + "k-" + Math.round(max / 1000) + "k";
  }

  function typeChip(type) {
    const cls = type === "实习" ? "chip-accent" : type === "校招" ? "chip-primary" : "chip-line";
    return `<span class="chip ${cls}">${esc(type)}</span>`;
  }

  function renderJobCard(job) {
    return `
    <a class="job-card" href="/job?id=${job.id}">
      <div class="job-card-top">
        <div class="job-card-logo">${esc(job.logo_text || String(job.company_name || "企").slice(0, 2))}</div>
        <div style="flex:1;min-width:0">
          <div class="job-card-title"><strong>${esc(job.title)}</strong>${typeChip(job.type)}${job.convertible ? '<span class="chip chip-success">可转正</span>' : ""}</div>
          <div class="job-card-company">${esc(job.company_name)} · ${esc(job.industry || "")}${job.company_size ? " · " + esc(job.company_size) : ""}</div>
        </div>
        <div class="job-card-pay"><strong>${salaryText(job.salary_min, job.salary_max)}</strong><span>${job.type === "实习" ? "日薪" : "月薪"}</span></div>
      </div>
      <div class="job-meta">
        <span class="chip">${esc(job.city)}</span>
        <span class="chip">${esc(job.degree)}</span>
        <span class="chip">${job.days_per_week >= 5 ? "每周 5 天" : "每周 " + job.days_per_week + " 天"}</span>
        <span class="chip chip-line">热度 ${esc(job.heat)}</span>
        <span class="chip chip-line">${esc(job.publish_date)}</span>
      </div>
    </a>`;
  }

  function renderHeader(activeKey) {
    const nav = NAV.map(n =>
      `<a href="${n.href}" class="${n.key === activeKey ? "active" : ""}">${n.label}</a>`).join("");
    return `
    <header class="site-header">
      <div class="container header-inner">
        <a class="brand" href="/">
          <span class="brand-mark">职</span>
          <span>大学生职业网<span class="brand-sub">CAMPUS CAREER</span></span>
        </a>
        <nav class="main-nav">${nav}</nav>
        <div class="header-user" data-user-slot>
          <a class="btn btn-primary btn-sm header-cta" href="/interns">立即投递</a>
        </div>
      </div>
    </header>`;
  }

  function renderUserArea(user) {
    const slot = document.querySelector("[data-user-slot]");
    if (!slot) return;
    if (!user) {
      slot.innerHTML = `<a class="btn btn-ghost btn-sm" href="/login">登录</a>`;
      return;
    }
    slot.innerHTML = `
      <span class="user-name" title="${esc(user.username)}">${esc(user.display_name || user.username)}</span>
      <button class="btn btn-ghost btn-sm" type="button" id="btnLogout">退出</button>`;
    const btn = document.getElementById("btnLogout");
    if (btn) btn.addEventListener("click", async () => {
      try { await api.logout(); } catch (e) { /* 忽略，直接跳登录页 */ }
      location.href = "/login";
    });
  }

  function renderFooter() {
    return `
    <footer class="site-footer">
      <div class="container">
        <div class="footer-grid">
          <div>
            <div class="footer-brand">大学生职业网 CampusCareer</div>
            <p>面向在校大学生的一站式线上职业服务平台，聚合求职信息、职业规划、简历制作、实习获取与求职工具，帮助每一位同学少走弯路。</p>
          </div>
          <div>
            <h4>核心模块</h4>
            <a href="/jobs">求职信息聚合</a>
            <a href="/plan">职业规划指导</a>
            <a href="/resume">简历制作</a>
            <a href="/interns">实习岗位获取</a>
            <a href="/toolbox">求职工具箱</a>
          </div>
          <div>
            <h4>项目资料</h4>
            <a href="/roadmap">版本迭代记录</a>
            <a href="/about">项目介绍</a>
            <a href="/about#research">调研与测试</a>
            <a href="/health">服务健康检查</a>
          </div>
          <div>
            <h4>使用提示</h4>
            <p style="margin:0 0 4px">站内岗位、企业与薪资信息均为教学演示数据，非真实招聘信息。</p>
            <p style="margin:0">CampusCareer V3.2</p>
          </div>
        </div>
        <div class="footer-bottom">
          大学生职业网 · 网站设计与开发实践项目 · V3.2 &nbsp;|&nbsp; 数据为模拟演示数据，仅供教学使用
        </div>
      </div>
    </footer>`;
  }

  function mountLayout(activeKey) {
    const headerSlot = document.querySelector("[data-header]");
    const footerSlot = document.querySelector("[data-footer]");
    if (headerSlot) headerSlot.outerHTML = renderHeader(activeKey);
    if (footerSlot) footerSlot.outerHTML = renderFooter();
    document.querySelectorAll("[data-year]").forEach(el => { el.textContent = new Date().getFullYear(); });
    api.me().then(renderUserArea).catch(() => {});
  }

  function debounce(fn, wait) {
    let timer = null;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), wait);
    };
  }

  function param(name) {
    return new URLSearchParams(location.search).get(name) || "";
  }

  window.App = { esc, api, toast, salaryText, typeChip, renderJobCard, mountLayout, debounce, param, request, renderUserArea };
})();
