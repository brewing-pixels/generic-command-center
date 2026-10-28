// Draft decomposition diagram: objectives as lanes, tasks as cards, depends_on
// as arrows. Pure DOM + SVG, no dependencies. Editing is delegated: the caller
// passes onSelect and does the work.
(function () {
  const CARD_W = 212;
  const CARD_H = 78;
  const GAP_X = 54;
  const GAP_Y = 26;
  const PAD = 18;
  const HEAD = 26; // objective heading strip above its cards
  const BAND_GAP = 18; // space between objective bands
  const BAND_PAD = 14; // breathing room under the last card row

  function layout(tree) {
    const index = new Map();
    tree.objectives.forEach((objective, lane) => {
      objective.tasks.forEach((task) => index.set(task.slug, { task, lane }));
    });
    const depth = new Map();
    const visit = (slug, seen) => {
      if (depth.has(slug)) return depth.get(slug);
      if (seen.has(slug)) return 0;
      seen.add(slug);
      const entry = index.get(slug);
      const deps = entry ? entry.task.depends_on || [] : [];
      const value = deps.length
        ? Math.max(...deps.map((dep) => visit(dep, seen) + 1))
        : 0;
      depth.set(slug, value);
      return value;
    };
    index.forEach((_, slug) => visit(slug, new Set()));

    // Objectives are horizontal bands; dependency depth runs left to right.
    // Cards that share a column stack downward inside their own band.
    const stacks = tree.objectives.map(() => new Map());
    const bands = [];
    const nodes = [];
    let top = PAD;
    let maxDepth = 0;
    tree.objectives.forEach((objective, lane) => {
      const taken = stacks[lane];
      const rows = [];
      objective.tasks.forEach((task) => {
        const column = depth.get(task.slug) || 0;
        const row = taken.get(column) || 0;
        taken.set(column, row + 1);
        rows.push(row + 1);
        maxDepth = Math.max(maxDepth, column);
        nodes.push({
          task,
          lane,
          column,
          row,
          x: PAD + column * (CARD_W + GAP_X),
          y: top + HEAD + row * (CARD_H + GAP_Y),
        });
      });
      const count = Math.max(1, ...rows);
      const height =
        HEAD + count * CARD_H + (count - 1) * GAP_Y + BAND_PAD;
      bands.push({ lane, objective, top, height });
      top += height + BAND_GAP;
    });
    const width = PAD * 2 + (maxDepth + 1) * CARD_W + maxDepth * GAP_X;
    const height = Math.max(top - BAND_GAP + PAD, PAD * 2);
    return { width, height, nodes, bands, index };
  }

  function edgePath(from, to) {
    const x1 = from.x + CARD_W;
    const y1 = from.y + CARD_H / 2;
    const x2 = to.x;
    const y2 = to.y + CARD_H / 2;
    if (x2 <= x1) {
      // Same column, or a cycle the depth guard could not order: loop out to
      // the right instead of running backwards through the cards.
      const side = Math.max(x1, x2) + 34;
      return `M${x1} ${y1} C${side} ${y1} ${side} ${y2} ${x2} ${y2}`;
    }
    const mid = Math.max(18, (x2 - x1) / 2);
    return `M${x1} ${y1} C${x1 + mid} ${y1} ${x2 - mid} ${y2} ${x2} ${y2}`;
  }

  function render(container, tree, options = {}) {
    const selected = options.selected || null;
    const data = layout(tree);
    container.innerHTML = "";
    const canvas = document.createElement("div");
    canvas.className = "dg-canvas";
    canvas.style.width = `${data.width}px`;
    canvas.style.height = `${data.height}px`;

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "dg-edges");
    svg.setAttribute("width", String(data.width));
    svg.setAttribute("height", String(data.height));
    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    defs.innerHTML =
      '<marker id="dg-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L8 4 L0 8 Z" fill="context-stroke"></path></marker>';
    svg.appendChild(defs);
    const bySlug = new Map(data.nodes.map((node) => [node.task.slug, node]));
    for (const node of data.nodes) {
      for (const dep of node.task.depends_on || []) {
        const from = bySlug.get(dep);
        if (!from) continue;
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", edgePath(from, node));
        path.setAttribute("class", "dg-edge");
        path.setAttribute("marker-end", "url(#dg-arrow)");
        path.dataset.from = dep;
        path.dataset.to = node.task.slug;
        if (selected && (selected === dep || selected === node.task.slug)) {
          path.classList.add("active");
        }
        svg.appendChild(path);
      }
    }

    data.bands.forEach((band, lane) => {
      const head = document.createElement("div");
      head.className = "dg-lane-head";
      head.style.left = `${PAD}px`;
      head.style.right = `${PAD}px`;
      head.style.top = `${band.top}px`;
      head.textContent = band.objective.title;
      head.title = band.objective.title;
      canvas.appendChild(head);
      if (lane > 0) {
        const rule = document.createElement("div");
        rule.className = "dg-band-rule";
        rule.style.top = `${band.top - BAND_GAP / 2 - 4}px`;
        canvas.appendChild(rule);
      }
    });

    canvas.appendChild(svg);

    for (const node of data.nodes) {
      const task = node.task;
      const card = document.createElement("button");
      card.type = "button";
      card.className = "dg-node";
      card.dataset.slug = task.slug;
      card.style.left = `${node.x}px`;
      card.style.top = `${node.y}px`;
      if (!(task.gate || []).length) card.classList.add("nogate");
      if (selected === task.slug) card.classList.add("active");
      const gate = (task.gate || []).length
        ? `<b${(task.gate[0] || []).length ? "" : ' class="bad"'}>gate</b>`
        : '<b class="bad">no gate</b>';
      card.innerHTML = `
        <span class="dg-title">${escapeHtml(task.title)}</span>
        <span class="dg-meta">${gate}<span>${(task.scope || []).length} scope</span><span>${(task.depends_on || []).length} dep</span></span>`;
      card.addEventListener("click", () => {
        if (options.onSelect) options.onSelect(task.slug);
      });
      canvas.appendChild(card);
    }

    container.appendChild(canvas);
    if (options.onReady) options.onReady(data);
    return data;
  }

  // Use-case flows: one horizontal activity band per scenario. UML notation:
  // an initial state, activities, a final state, and the actor in the partition
  // header.
  const FLOW_NODE = 18;
  const FLOW_LEAD = 46;

  function actorGlyph() {
    const span = document.createElement("span");
    span.className = "dg-actor";
    span.innerHTML =
      '<svg viewBox="0 0 12 18" aria-hidden="true"><circle cx="6" cy="3" r="2.3"></circle><path d="M6 5.4v6M2 8h8M6 11.4 3 17M6 11.4 9 17"></path></svg>';
    return span;
  }

  function straightEdge(svg, x1, y1, x2, y2) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M${x1} ${y1} L${x2} ${y2}`);
    path.setAttribute("class", "dg-edge");
    path.setAttribute("marker-end", "url(#dg-arrow)");
    svg.appendChild(path);
    return path;
  }

  function renderFlows(container, doc, options = {}) {
    const cases = (doc?.use_cases || []).filter(
      (useCase) => !options.only || useCase.id === options.only
    );
    const titles = new Map();
    for (const objective of options.tasks?.objectives || []) {
      for (const task of objective.tasks) titles.set(task.slug, task.title);
    }
    const selected = options.selected || null;
    const collapsed = options.collapsed || new Set();
    const expanded = cases.filter((useCase) => !collapsed.has(useCase.id));
    const firstX = PAD + FLOW_LEAD;
    const endOf = (count) =>
      firstX + (count - 1) * (CARD_W + GAP_X) + CARD_W + 24;
    const width = Math.max(
      420,
      ...expanded.map(
        (useCase) => endOf(useCase.steps.length) + FLOW_NODE + PAD
      )
    );
    const bands = [];
    let top = PAD;
    for (const useCase of cases) {
      const isCollapsed = collapsed.has(useCase.id);
      const branching = useCase.steps.some(
        (step) => step.kind === "choice" && (step.branches || []).length
      );
      const height = isCollapsed
        ? HEAD + 10
        : HEAD + CARD_H + (branching ? 44 : 0) + BAND_PAD;
      bands.push({ useCase, top, height, collapsed: isCollapsed });
      top += height + BAND_GAP;
    }
    const height = Math.max(top - BAND_GAP + PAD, PAD * 2);

    container.innerHTML = "";
    const canvas = document.createElement("div");
    canvas.className = "dg-canvas";
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "dg-edges");
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(height));
    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    defs.innerHTML =
      '<marker id="dg-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L8 4 L0 8 Z" fill="context-stroke"></path></marker>';
    svg.appendChild(defs);
    canvas.appendChild(svg);

    bands.forEach((band, lane) => {
      const head = document.createElement("div");
      head.className = `dg-lane-head flow-head${
        band.collapsed ? "" : " expanded"
      }`;
      head.style.left = `${PAD}px`;
      head.style.right = `${PAD}px`;
      head.style.top = `${band.top}px`;
      const chevron = document.createElement("span");
      chevron.className = "dg-collapse";
      chevron.textContent = "▸";
      head.appendChild(chevron);
      if (band.useCase.actor) head.appendChild(actorGlyph());
      const title = document.createElement("span");
      title.className = "dg-lane-title";
      title.textContent = band.useCase.title;
      head.appendChild(title);
      const count = document.createElement("span");
      count.className = "dg-flow-count";
      const total = band.useCase.steps.length;
      count.textContent = `${total} step${total === 1 ? "" : "s"}`;
      head.appendChild(count);
      head.title = band.useCase.summary || band.useCase.title;
      head.addEventListener("click", () => {
        if (options.onToggle) options.onToggle(band.useCase.id);
      });
      canvas.appendChild(head);
      if (lane > 0) {
        const rule = document.createElement("div");
        rule.className = "dg-band-rule";
        rule.style.top = `${band.top - BAND_GAP / 2 - 4}px`;
        canvas.appendChild(rule);
      }

      if (band.collapsed) return;

      const rowY = band.top + HEAD;
      const midY = rowY + CARD_H / 2;
      const endX = endOf(band.useCase.steps.length);

      const startNode = document.createElement("span");
      startNode.className = "dg-flow-start";
      startNode.style.left = `${PAD}px`;
      startNode.style.top = `${midY - FLOW_NODE / 2}px`;
      startNode.title = "start";
      canvas.appendChild(startNode);

      const endNode = document.createElement("span");
      endNode.className = "dg-flow-end";
      endNode.style.left = `${endX - FLOW_NODE / 2}px`;
      endNode.style.top = `${midY - FLOW_NODE / 2}px`;
      endNode.title = "end";
      canvas.appendChild(endNode);

      straightEdge(
        svg,
        PAD + FLOW_NODE,
        midY,
        firstX,
        midY
      );

      const indexById = new Map();
      band.useCase.steps.forEach((row, position) => {
        if (row.id) indexById.set(row.id, position);
      });
      const dipY = rowY + CARD_H + 26;

      band.useCase.steps.forEach((step, index) => {
        const x = firstX + index * (CARD_W + GAP_X);
        const isChoice = step.kind === "choice";
        const card = document.createElement("button");
        card.type = "button";
        card.className = "dg-node flow-node";
        if (isChoice) card.classList.add("choice");
        card.dataset.uc = band.useCase.id;
        card.dataset.step = String(index);
        card.style.left = `${x}px`;
        card.style.top = `${rowY}px`;
        const isSelected =
          selected && selected.uc === band.useCase.id && selected.index === index;
        if (isSelected) card.classList.add("active");
        const linked = step.task ? titles.get(step.task) : null;
        if (isChoice) {
          const count = (step.branches || []).length;
          card.innerHTML = `
            <span class="dg-diamond" aria-hidden="true"></span>
            <span class="dg-choice">
              <span class="dg-title">${escapeHtml(step.label)}</span>
              <span class="dg-meta"><b class="${count ? "" : "bad"}">${
                count ? `${count} branch${count === 1 ? "" : "es"}` : "no branch"
              }</b></span>
            </span>`;
        } else {
          card.innerHTML = `
            <span class="dg-title">${escapeHtml(step.label)}</span>
            <span class="dg-meta"><b class="${step.task ? "" : "bad"}">${index + 1}</b>
              <span>${escapeHtml(step.task ? linked || step.task : "no task")}</span></span>`;
        }
        card.addEventListener("click", () => {
          if (options.onSelect) options.onSelect(band.useCase.id, index);
        });
        canvas.appendChild(card);

        const previous = band.useCase.steps[index - 1];
        const previousBranches =
          previous &&
          previous.kind === "choice" &&
          (previous.branches || []).length > 0;
        if (index > 0 && !previousBranches) {
          const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
          path.setAttribute(
            "d",
            edgePath({ x: x - GAP_X - CARD_W, y: rowY }, { x, y: rowY })
          );
          path.setAttribute("class", "dg-edge");
          path.setAttribute("marker-end", "url(#dg-arrow)");
          if (isSelected) path.classList.add("active");
          svg.appendChild(path);
        }
        if (index === band.useCase.steps.length - 1) {
          straightEdge(svg, x + CARD_W, midY, endX - FLOW_NODE / 2, midY);
        }

        if (isChoice && (step.branches || []).length) {
          const sx = x + 28;
          const sy = rowY + CARD_H;
          for (const branch of step.branches) {
            const targetIndex = indexById.get(branch.to);
            if (targetIndex == null) continue;
            const tx = firstX + targetIndex * (CARD_W + GAP_X) + CARD_W / 2;
            const radius = 10;
            const path = document.createElementNS(
              "http://www.w3.org/2000/svg",
              "path"
            );
            path.setAttribute(
              "d",
              `M${sx} ${sy} V${dipY - radius} Q${sx} ${dipY} ${sx + radius} ${dipY} ` +
                `H${tx - radius} Q${tx} ${dipY} ${tx} ${dipY - radius} V${sy + 3}`
            );
            path.setAttribute("class", "dg-edge branch");
            path.setAttribute("marker-end", "url(#dg-arrow)");
            svg.appendChild(path);
            const label = document.createElementNS(
              "http://www.w3.org/2000/svg",
              "text"
            );
            label.setAttribute("x", String(tx - 8));
            label.setAttribute("y", String(dipY - 5));
            label.setAttribute("text-anchor", "end");
            label.setAttribute("class", "dg-guard");
            label.textContent = `[${branch.guard}]`;
            svg.appendChild(label);
          }
        }
      });
    });

    container.appendChild(canvas);
    return { width, height, bands };
  }

  // C4 levels: boxes laid out left to right by relationship depth.
  const C4_W = 232;
  const C4_H = 92;
  const C4_GAP_X = 74;
  const C4_GAP_Y = 26;

  const C4_GLYPHS = {
    person: '<circle cx="10" cy="4" r="2.6"></circle><path d="M10 7v7M5 10h10M10 14 6 20M10 14 14 20"></path>',
    system: '<rect x="3" y="5" width="14" height="10" rx="1"></rect>',
    external: '<rect x="3" y="5" width="14" height="10" rx="1" stroke-dasharray="3 2"></rect>',
    container: '<rect x="2" y="3" width="16" height="14" rx="3"></rect><rect x="6" y="7" width="8" height="6" rx="1"></rect>',
    component: '<rect x="2" y="5" width="16" height="10" rx="1"></rect><path d="M6 5V2M12 5V2M6 20v-3M12 20v-3"></path>',
    database: '<ellipse cx="10" cy="5" rx="7" ry="2.6"></ellipse><path d="M3 5v10c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V5"></path>',
    queue: '<rect x="2" y="6" width="16" height="8" rx="1"></rect><path d="M6 6v8M14 6v8"></path>',
    code: '<rect x="3" y="3" width="14" height="14" rx="1"></rect><path d="M3 8h14"></path>',
    decision: '<path d="M10 2l8 8-8 8-8-8z"></path>',
  };

  function c4Glyph(type) {
    const span = document.createElement("span");
    span.className = "c4-glyph";
    span.innerHTML =
      `<svg viewBox="0 0 20 22" aria-hidden="true">` +
      (C4_GLYPHS[type] || C4_GLYPHS.system) +
      `</svg>`;
    return span;
  }

  function renderC4(container, level, options = {}) {
    const elements = level?.elements || [];
    const relationships = level?.relationships || [];
    const byId = new Map(elements.map((element, index) => [element.id, index]));
    const incoming = new Map(elements.map((element) => [element.id, []]));
    const ends = (relationship) => ({
      from: relationship.from ?? relationship.from_,
      to: relationship.to,
    });
    for (const relationship of relationships) {
      incoming.get(relationship.to)?.push(ends(relationship).from);
    }
    const depth = new Map();
    const visit = (id, seen) => {
      if (depth.has(id)) return depth.get(id);
      if (seen.has(id)) return 0;
      seen.add(id);
      const parents = incoming.get(id) || [];
      const value = parents.length
        ? Math.max(...parents.map((parent) => visit(parent, seen) + 1))
        : 0;
      depth.set(id, value);
      return value;
    };
    elements.forEach((element) => visit(element.id, new Set()));
    const columns = new Map();
    const positions = new Map();
    let maxDepth = 0;
    let maxRow = 0;
    elements.forEach((element, index) => {
      const column = depth.get(element.id) || 0;
      const row = columns.get(column) || 0;
      columns.set(column, row + 1);
      maxDepth = Math.max(maxDepth, column);
      maxRow = Math.max(maxRow, row);
      positions.set(element.id, {
        element,
        index,
        x: PAD + column * (C4_W + C4_GAP_X),
        y: PAD + row * (C4_H + C4_GAP_Y),
      });
    });
    const width = PAD * 2 + (maxDepth + 1) * C4_W + maxDepth * C4_GAP_X;
    const height = PAD * 2 + (maxRow + 1) * C4_H + maxRow * C4_GAP_Y;

    container.innerHTML = "";
    const canvas = document.createElement("div");
    canvas.className = "dg-canvas";
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "dg-edges");
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(height));
    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    defs.innerHTML =
      '<marker id="dg-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L8 4 L0 8 Z" fill="context-stroke"></path></marker>';
    svg.appendChild(defs);
    canvas.appendChild(svg);

    for (const relationship of relationships) {
      const from = positions.get(ends(relationship).from);
      const to = positions.get(ends(relationship).to);
      if (!from || !to) continue;
      const x1 = from.x + C4_W;
      const y1 = from.y + C4_H / 2;
      const x2 = to.x;
      const y2 = to.y + C4_H / 2;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      if (x2 <= x1) {
        const side = x1 + 40;
        path.setAttribute(
          "d",
          `M${x1} ${y1} C${side} ${y1} ${side} ${y2} ${x2} ${y2}`
        );
      } else {
        const mid = Math.max(18, (x2 - x1) / 2);
        path.setAttribute(
          "d",
          `M${x1} ${y1} C${x1 + mid} ${y1} ${x2 - mid} ${y2} ${x2} ${y2}`
        );
      }
      path.setAttribute("class", "dg-edge");
      path.setAttribute("marker-end", "url(#dg-arrow)");
      svg.appendChild(path);
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", String((x1 + x2) / 2 + (x2 <= x1 ? 30 : 0)));
      label.setAttribute("y", String((y1 + y2) / 2 - 5));
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("class", "dg-rel");
      label.textContent = relationship.technology
        ? `${relationship.label} [${relationship.technology}]`
        : relationship.label;
      svg.appendChild(label);
    }

    for (const position of positions.values()) {
      const { element, index } = position;
      const card = document.createElement("button");
      card.type = "button";
      card.className = `dg-node c4-node type-${element.type}`;
      card.dataset.id = element.id;
      card.dataset.index = String(index);
      card.style.left = `${position.x}px`;
      card.style.top = `${position.y}px`;
      if (options.selected === element.id) card.classList.add("active");
      if (element.type === "decision") {
        card.classList.add("decision");
        card.innerHTML = `
          <span class="c4-diamond" aria-hidden="true"></span>
          <span class="c4-body">
            <span class="dg-title">${escapeHtml(element.name)}</span>
            <span class="c4-meta">decision</span>
          </span>`;
      } else {
        card.innerHTML =
          `<span class="c4-body">
             <span class="dg-title">${escapeHtml(element.name)}</span>
             ${element.technology ? `<span class="c4-tech">[${escapeHtml(element.technology)}]</span>` : ""}
             ${element.description ? `<span class="c4-desc">${escapeHtml(element.description)}</span>` : ""}
           </span>`;
        card.prepend(c4Glyph(element.type));
      }
      card.addEventListener("click", () => {
        if (options.onSelect) options.onSelect(element.id, index);
      });
      canvas.appendChild(card);
    }

    container.appendChild(canvas);
    return { width, height };
  }

  // The builder's process: the day's tasks in order, one node each.
  const PROC_W = 224;
  const PROC_H = 78;
  const PROC_GAP = 46;

  function renderProcess(container, items, options = {}) {
    const width = PAD * 2 + items.length * PROC_W + Math.max(0, items.length - 1) * PROC_GAP;
    const height = PAD * 2 + PROC_H;
    container.innerHTML = "";
    const canvas = document.createElement("div");
    canvas.className = "dg-canvas";
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "dg-edges");
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(height));
    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    defs.innerHTML =
      '<marker id="dg-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L8 4 L0 8 Z" fill="context-stroke"></path></marker>';
    svg.appendChild(defs);
    canvas.appendChild(svg);

    items.forEach((item, index) => {
      const x = PAD + index * (PROC_W + PROC_GAP);
      const y = PAD;
      if (index > 0) {
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute(
          "d",
          `M${x - PROC_GAP + 8} ${y + PROC_H / 2} L${x - 8} ${y + PROC_H / 2}`
        );
        path.setAttribute("class", "dg-edge");
        path.setAttribute("marker-end", "url(#dg-arrow)");
        svg.appendChild(path);
      }
      const node = document.createElement("button");
      node.type = "button";
      node.className = `dg-node process-node status-${item.status}`;
      node.dataset.task = item.task;
      node.dataset.run = item.run || "";
      node.style.left = `${x}px`;
      node.style.top = `${y}px`;
      if (options.selected && options.selected === item.run) {
        node.classList.add("active");
      }
      const dot =
        item.status === "running"
          ? '<span class="proc-spin" aria-hidden="true"></span>'
          : `<span class="proc-dot" aria-hidden="true"></span>`;
      node.innerHTML = `
        <span class="proc-head">${dot}<span class="dg-title">${escapeHtml(
          item.title
        )}</span></span>
        <span class="proc-meta">${escapeHtml(item.meta || "")}</span>`;
      node.addEventListener("click", () => {
        if (options.onSelect) options.onSelect(item);
      });
      canvas.appendChild(node);
    });

    container.appendChild(canvas);
    return { width, height };
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  window.renderDraftDiagram = render;
  window.renderUseCaseDiagram = renderFlows;
  window.renderC4Diagram = renderC4;
  window.renderProcessDiagram = renderProcess;
})();
