function workoutBuilder(initialDefinition, initialSport) {
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const definition = clone(initialDefinition);
  const expanded = [];
  const makeId = () => window.crypto?.randomUUID?.() || `block-${Date.now()}-${Math.random()}`;

  return {
    definition,
    sport: initialSport,
    expanded,
    dragSource: null,
    dropTarget: null,
    isDragging: false,
    ignorePaletteClick: false,
    announcement: "",

    serializedDefinition() {
      return JSON.stringify(this.definition);
    },
    newStep(stepType = "interval") {
      return {
        id: makeId(),
        kind: "step",
        step_type: stepType,
        end: { type: "time", seconds: stepType === "recovery" ? 60 : 300 },
        target: { type: "none" },
      };
    },
    addStep(stepType, parentId = null) {
      const step = this.newStep(stepType);
      this.container(parentId).push(step);
      this.expanded.push(step.id);
      this.announcement = `${this.blockTitle(step)} hinzugefügt.`;
    },
    newRepeat() {
      return {
        id: makeId(),
        kind: "repeat",
        iterations: 8,
        children: [this.newStep("interval"), this.newStep("recovery")],
      };
    },
    addRepeat() {
      const repeat = this.newRepeat();
      this.definition.blocks.push(repeat);
      this.expanded.push(repeat.id);
      this.announcement = "Wiederholung mit Belastung und Erholung hinzugefügt.";
    },
    container(parentId) {
      if (!parentId) return this.definition.blocks;
      const parent = this.definition.blocks.find((block) => block.id === parentId);
      return parent?.kind === "repeat" ? parent.children : [];
    },
    repeatBlocks() {
      return this.definition.blocks.filter((block) => block.kind === "repeat");
    },
    move(parentId, index, offset) {
      const list = this.container(parentId);
      const destination = index + offset;
      if (destination < 0 || destination >= list.length) return;
      const [block] = list.splice(index, 1);
      list.splice(destination, 0, block);
      this.announcement = `${this.blockTitle(block)} verschoben.`;
    },
    moveIntoRepeat(index, repeatId) {
      if (!repeatId) return;
      const [block] = this.definition.blocks.splice(index, 1);
      const repeat = this.definition.blocks.find((candidate) => candidate.id === repeatId);
      if (!block || block.kind === "repeat" || !repeat) {
        if (block) this.definition.blocks.splice(index, 0, block);
        return;
      }
      repeat.children.push(block);
      this.announcement = `${this.blockTitle(block)} in Wiederholung verschoben.`;
    },
    moveOut(parentId, index) {
      const repeatIndex = this.definition.blocks.findIndex((block) => block.id === parentId);
      if (repeatIndex < 0) return;
      const [block] = this.container(parentId).splice(index, 1);
      this.definition.blocks.splice(repeatIndex + 1, 0, block);
      this.announcement = `${this.blockTitle(block)} aus Wiederholung gelöst.`;
    },
    duplicate(parentId, index) {
      const list = this.container(parentId);
      const copy = clone(list[index]);
      this.refreshIds(copy);
      list.splice(index + 1, 0, copy);
      this.announcement = `${this.blockTitle(copy)} dupliziert.`;
    },
    refreshIds(block) {
      block.id = makeId();
      if (block.kind === "repeat") block.children.forEach((child) => this.refreshIds(child));
    },
    remove(parentId, index) {
      const list = this.container(parentId);
      const block = list[index];
      if (block.kind === "repeat" && block.children.length && !window.confirm("Wiederholung mit allen enthaltenen Schritten entfernen?")) return;
      list.splice(index, 1);
      this.announcement = `${this.blockTitle(block)} entfernt.`;
    },
    startDrag(parentId, index, event) {
      this.dragSource = { origin: "flow", parentId, index };
      this.isDragging = true;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", `${parentId || "root"}:${index}`);
    },
    startPaletteDrag(blockType, event) {
      this.dragSource = { origin: "palette", blockType };
      this.isDragging = true;
      this.ignorePaletteClick = true;
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.setData("text/plain", `palette:${blockType}`);
    },
    endDrag() {
      const paletteDrag = this.dragSource?.origin === "palette";
      this.dragSource = null;
      this.dropTarget = null;
      this.isDragging = false;
      if (paletteDrag) window.setTimeout(() => { this.ignorePaletteClick = false; }, 0);
    },
    canDrop(parentId) {
      if (!this.dragSource) return false;
      if (!parentId) return true;
      if (this.dragSource.origin === "palette") return this.dragSource.blockType !== "repeat";
      const source = this.container(this.dragSource.parentId)[this.dragSource.index];
      return source?.kind !== "repeat";
    },
    dropKey(parentId, index) {
      return `${parentId || "root"}:${index}`;
    },
    setDropTarget(parentId, index) {
      if (this.canDrop(parentId)) this.dropTarget = this.dropKey(parentId, index);
    },
    isDropTarget(parentId, index) {
      return this.dropTarget === this.dropKey(parentId, index);
    },
    dropAt(parentId, index) {
      if (!this.dragSource || !this.canDrop(parentId)) return;
      const source = this.dragSource;
      if (source.origin === "palette") {
        const block = source.blockType === "repeat" ? this.newRepeat() : this.newStep(source.blockType);
        this.container(parentId).splice(index, 0, block);
        this.expanded.push(block.id);
        this.announcement = `${this.blockTitle(block)} hinzugefügt.`;
        this.endDrag();
        return;
      }
      const sourceList = this.container(source.parentId);
      const [block] = sourceList.splice(source.index, 1);
      if (!block || (parentId && block.kind === "repeat")) {
        if (block) sourceList.splice(source.index, 0, block);
        this.endDrag();
        return;
      }
      const targetList = this.container(parentId);
      if (source.parentId === parentId && source.index < index) index -= 1;
      targetList.splice(index, 0, block);
      this.announcement = `${this.blockTitle(block)} verschoben.`;
      this.endDrag();
    },
    toggle(id) {
      const index = this.expanded.indexOf(id);
      index >= 0 ? this.expanded.splice(index, 1) : this.expanded.push(id);
    },
    isExpanded(id) {
      return this.expanded.includes(id);
    },
    setEndType(step, type) {
      step.end = type === "time" ? { type: "time", seconds: 300 } : { type: "distance", meters: 1000 };
    },
    setTargetType(step, type) {
      const targets = {
        none: { type: "none" },
        pace_range: { type: "pace_range", fastest_seconds_per_km: 300, slowest_seconds_per_km: 330 },
        heart_rate_range: { type: "heart_rate_range", lower_bpm: 120, upper_bpm: 150 },
        heart_rate_zone: { type: "heart_rate_zone", zone: 2 },
      };
      step.target = targets[type];
    },
    minutes(step) {
      return Math.round((step.end.seconds / 60) * 10) / 10;
    },
    formatPace(seconds) {
      const value = Math.round(Number(seconds) || 0);
      return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
    },
    setPace(target, field, value) {
      const match = /^(\d+):([0-5]\d)$/.exec(value);
      if (match) target[field] = Number(match[1]) * 60 + Number(match[2]);
    },
    blockIcon(block) {
      if (block.kind === "repeat") return "repeat";
      return { warmup: "sunrise", interval: "bolt", recovery: "pause", cooldown: "flag" }[block.step_type];
    },
    blockTitle(block) {
      if (block.kind === "repeat") return `${block.iterations}× wiederholen`;
      return { warmup: "Aufwärmen", interval: "Belastung", recovery: "Erholung", cooldown: "Auslaufen" }[block.step_type];
    },
    blockSummary(block) {
      if (block.kind === "repeat") return `${block.children.length} Schritte im Block`;
      const end = block.end.type === "time" ? `${this.minutes(block)} min` : `${block.end.meters} m`;
      const target = block.target.type === "pace_range"
        ? `Pace ${this.formatPace(block.target.fastest_seconds_per_km)}–${this.formatPace(block.target.slowest_seconds_per_km)}`
        : block.target.type === "heart_rate_range"
          ? `HF ${block.target.lower_bpm}–${block.target.upper_bpm} bpm`
          : block.target.type === "heart_rate_zone" ? `HF-Zone ${block.target.zone}` : "Kein Ziel";
      return `${end} · ${target}`;
    },
    leafCount() {
      return this.definition.blocks.reduce((count, block) => count + (block.kind === "repeat" ? block.children.length : 1), 0);
    },
    repeatCount() {
      return this.repeatBlocks().length;
    },
  };
}
