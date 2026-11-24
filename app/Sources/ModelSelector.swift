import AppKit
import SwiftUI

/// Local hover & per-manufacturer memory for one `ModelSelector` instance (no `@State`: see Config.swift).
final class ModelSelectorState: ObservableObject {
    @Published var hoveredMfg: ModelManufacturer?
    @Published var hoveredChoice: String?
    var lastByMfg = [ModelManufacturer: String]()
}

/// Reusable 3-step Settings model selector: Manufacturer buttons (with icons) -> Model buttons (with icons) -> Effort slider.
/// Follows Greenlight's design tokens (brand/DESIGN.md §§2–6, §10.20) with zero literal colours, sizes, or radii.
struct ModelSelector: View {
    @ObservedObject var c: Core
    let title: String
    var hint: String? = nil
    let model: Binding<String>
    let effort: Binding<String>
    var allowedHarnesses: Set<String>? = nil
    @StateObject private var ui = ModelSelectorState()

    init(c: Core, title: String, hint: String? = nil, model: Binding<String>, effort: Binding<String>, allowedHarnesses: Set<String>? = nil) {
        self._c = ObservedObject(wrappedValue: c)
        self.title = title
        self.hint = hint
        self.model = model
        self.effort = effort
        self.allowedHarnesses = allowedHarnesses
    }

    var body: some View {
        let _ = c.look
        let keep: (String) -> Bool = { id in
            guard let allowed = allowedHarnesses else { return true }
            return allowed.contains(harnessOf(id))
        }
        let catalog = buildModelCatalog(from: c.availableModels, current: model.wrappedValue, where: keep)
        let active = activeModelSelection(in: catalog, modelId: model.wrappedValue)
        let activeMfg = active?.group.manufacturer
        let activeChoice = active?.choice
        let activeEffort = activeChoice.map {
            currentModelEffort(for: $0, modelId: model.wrappedValue, storedEffort: effort.wrappedValue)
        } ?? effort.wrappedValue

        VStack(alignment: .leading, spacing: DS.Space.s12) {
            VStack(alignment: .leading, spacing: DS.Space.s2) {
                Text(title).font(ds(DS.TypeRole.strong)).foregroundColor(fg)
                if let hint {
                    Text(hint).font(ds(DS.TypeRole.caption)).foregroundColor(dim).fixedSize(horizontal: false, vertical: true)
                }
            }
            if !catalog.isEmpty {
                favoritesSection(c.modelFavorites.filter { keep($0.model) }, catalog: catalog, activeEffort: activeEffort)
                VStack(alignment: .leading, spacing: DS.Space.s8) {
                    Text("Provider").font(ds(DS.TypeRole.labelSm)).foregroundColor(dim)
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 112), spacing: DS.Space.s8)], spacing: DS.Space.s8) {
                        ForEach(catalog) { group in
                            manufacturerButton(group, selected: group.manufacturer == activeMfg,
                                               currentChoice: activeChoice, desiredEffort: activeEffort)
                        }
                    }
                }
            }
            if let group = active?.group {
                VStack(alignment: .leading, spacing: DS.Space.s8) {
                    Text("Model").font(ds(DS.TypeRole.labelSm)).foregroundColor(dim)
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 160), spacing: DS.Space.s8)], spacing: DS.Space.s8) {
                        ForEach(group.choices) { choice in
                            choiceButton(choice, selected: choice.id == activeChoice?.id, desiredEffort: activeEffort)
                                .overlay(alignment: .trailing) { starButton(choice, desiredEffort: activeEffort) }
                        }
                    }
                }
            }
            if let choice = activeChoice {
                effortSection(for: choice, activeEffort: activeEffort)
            }
        }
        .padding(.horizontal, DS.Space.s16)
        .padding(.vertical, DS.Space.s12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .onAppear(perform: c.loadModels)
    }

    private func manufacturerButton(_ group: ModelGroup, selected: Bool,
                                    currentChoice: ModelChoice?, desiredEffort: String) -> some View {
        let mfg = group.manufacturer
        let hovered = ui.hoveredMfg == mfg
        let shape = RoundedRectangle(cornerRadius: DS.Radius.sm)
        return Button {
            guard !selected, let fallback = group.choices.first else { return }
            if let currentChoice { ui.lastByMfg[currentChoice.manufacturer] = currentChoice.id }
            let target = ui.lastByMfg[mfg].flatMap { savedId in group.choices.first { $0.id == savedId } } ?? fallback
            applyChoice(target, desiredEffort: desiredEffort)
        } label: {
            HStack(spacing: DS.Space.s8) {
                Image(ds: mfg.icon, size: iconSize(DS.TypeRole.body.size))
                    .foregroundColor(selected ? Color(accentSet.fg, dark: isDark) : hovered ? fg : sub)
                    .frame(width: DS.Space.s16)
                    .accessibilityHidden(true)
                Text(mfg.name)
                    .font(ds(DS.TypeRole.label))
                    .foregroundColor(selected ? Color(accentSet.fg, dark: isDark) : fg)
                    .lineLimit(1)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, DS.Space.s12)
            .frame(height: DS.Layout.controlHeightLg)
            .contentShape(shape)
            .background(shape.fill(selected ? Color(accentSet.subtle, dark: isDark)
                                            : hovered ? Color(DS.Palette.elementHover, dark: isDark) : surface))
            .overlay(shape.strokeBorder(selected ? Color(accentSet.ring, dark: isDark) : hovered ? edge : line))
        }
        .buttonStyle(.plain)
        .onHover { on in
            if on { ui.hoveredMfg = mfg } else if ui.hoveredMfg == mfg { ui.hoveredMfg = nil }
        }
        .accessibilityLabel("\(mfg.name) provider\(selected ? ", selected" : "")")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private func choiceButton(_ choice: ModelChoice, selected: Bool, desiredEffort: String) -> some View {
        let hovered = ui.hoveredChoice == choice.id
        let shape = RoundedRectangle(cornerRadius: DS.Radius.sm)
        let resolvedId = choice.resolve(desiredEffort: desiredEffort).modelId
        return Button {
            ui.lastByMfg[choice.manufacturer] = choice.id
            applyChoice(choice, desiredEffort: desiredEffort)
        } label: {
            HStack(spacing: DS.Space.s8) {
                Image(ds: choice.icon, size: iconSize(DS.TypeRole.label.size))
                    .foregroundColor(selected ? Color(accentSet.fg, dark: isDark) : hovered ? fg : sub)
                    .frame(width: DS.Space.s16)
                    .accessibilityHidden(true)
                Text(choice.name)
                    .font(ds(DS.TypeRole.label))
                    .foregroundColor(selected ? Color(accentSet.fg, dark: isDark) : fg)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer(minLength: 0)
            }
            .padding(.leading, DS.Space.s12)
            .padding(.trailing, DS.Space.s32)  // room for the star overlaid on the tile's trailing edge
            .frame(height: DS.Layout.controlHeightMd)
            .contentShape(shape)
            .background(shape.fill(selected ? Color(accentSet.subtle, dark: isDark)
                                            : hovered ? Color(DS.Palette.elementHover, dark: isDark) : surface))
            .overlay(shape.strokeBorder(selected ? Color(accentSet.ring, dark: isDark) : hovered ? edge : line))
        }
        .buttonStyle(.plain)
        .help("\(resolvedId) · \(choice.harness)")
        .onHover { on in
            if on { ui.hoveredChoice = choice.id } else if ui.hoveredChoice == choice.id { ui.hoveredChoice = nil }
        }
        .accessibilityLabel("\(choice.name)\(selected ? ", selected" : "")")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    /// Starred model + effort pairs (shared by every selector): one click sets both; right-click removes one.
    @ViewBuilder
    private func favoritesSection(_ favs: [ModelFavorite], catalog: [ModelGroup], activeEffort: String) -> some View {
        VStack(alignment: .leading, spacing: DS.Space.s8) {
            Text("Favorites").font(ds(DS.TypeRole.labelSm)).foregroundColor(dim)
            if favs.isEmpty {
                HStack(spacing: DS.Space.s4) {
                    Image(ds: "star", size: iconSize(DS.TypeRole.caption.size)).foregroundColor(faint).accessibilityHidden(true)
                    Text("Star a model to keep it here at its effort, one click away in every model setting.")
                        .font(ds(DS.TypeRole.caption)).foregroundColor(dim).fixedSize(horizontal: false, vertical: true)
                }
            } else {
                FlowRow(spacing: DS.Space.s8) {
                    ForEach(favs, id: \.self) { f in favoriteChip(f, catalog: catalog, activeEffort: activeEffort) }
                }
            }
        }
    }

    private func favoriteChip(_ f: ModelFavorite, catalog: [ModelGroup], activeEffort: String) -> some View {
        let selected = model.wrappedValue == f.model && (f.effort.isEmpty || f.effort == activeEffort)
        let label = modelFavoriteLabel(f, in: catalog)
        let shape = RoundedRectangle(cornerRadius: DS.Radius.sm)
        return Button {
            if let hit = activeModelSelection(in: catalog, modelId: f.model), hit.choice.matches(modelId: f.model) {
                ui.lastByMfg[hit.choice.manufacturer] = hit.choice.id
            }
            if model.wrappedValue != f.model { model.wrappedValue = f.model }
            if !f.effort.isEmpty, effort.wrappedValue != f.effort { effort.wrappedValue = f.effort }
        } label: {
            HStack(spacing: DS.Space.s8) {
                Image(ds: ModelManufacturer.of(f.model).icon, size: iconSize(DS.TypeRole.label.size))
                    .foregroundColor(selected ? Color(accentSet.fg, dark: isDark) : sub)
                    .accessibilityHidden(true)
                Text(label).font(ds(DS.TypeRole.label))
                    .foregroundColor(selected ? Color(accentSet.fg, dark: isDark) : fg)
                    .lineLimit(1).truncationMode(.tail)
            }
            .padding(.horizontal, DS.Space.s8)
            .frame(height: DS.Layout.controlHeightSm)
            .contentShape(shape)
            .background(shape.fill(selected ? Color(accentSet.subtle, dark: isDark) : surface))
            .overlay(shape.strokeBorder(selected ? Color(accentSet.ring, dark: isDark) : line))
        }
        .buttonStyle(.plain)
        .help(f.effort.isEmpty ? f.model : "\(f.model) · \(effortName(f.effort))")
        .contextMenu { Button("Remove from favorites") { c.modelFavorites.removeAll { $0 == f } } }
        .accessibilityLabel("Favorite \(label)\(selected ? ", selected" : "")")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    /// Stars `choice` at the effort the slider shows (filled when that exact pair is a favorite).
    private func starButton(_ choice: ModelChoice, desiredEffort: String) -> some View {
        let fav = modelFavorite(for: choice, desiredEffort: desiredEffort)
        let on = c.modelFavorites.contains(fav)
        let what = fav.effort.isEmpty ? choice.name : "\(choice.name) at \(effortName(fav.effort))"
        return Button { c.modelFavorites = togglingFavorite(fav, in: c.modelFavorites) } label: {
            Image(ds: on ? "star-fill" : "star", size: iconSize(DS.TypeRole.label.size))
                .foregroundColor(on ? fg : faint)
                .frame(width: DS.Layout.controlHeightSm, height: DS.Layout.controlHeightSm)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.trailing, DS.Space.s4)
        .help(on ? "Remove \(what) from favorites" : "Add \(what) to favorites")
        .accessibilityLabel(on ? "Remove \(what) from favorites" : "Add \(what) to favorites")
    }

    @ViewBuilder
    private func effortSection(for choice: ModelChoice, activeEffort: String) -> some View {
        VStack(alignment: .leading, spacing: DS.Space.s4) {
            HStack(alignment: .firstTextBaseline, spacing: DS.Space.s8) {
                Text("Effort").font(ds(DS.TypeRole.labelSm)).foregroundColor(dim)
                Spacer(minLength: DS.Space.s8)
                if choice.supportsEffortSlider {
                    Text(effortName(activeEffort)).font(ds(DS.TypeRole.label)).foregroundColor(fg)
                } else if let fixed = choice.fixedEffort {
                    Text("\(effortName(fixed)) (fixed)").font(ds(DS.TypeRole.label)).foregroundColor(dim)
                } else {
                    Text("Not used").font(ds(DS.TypeRole.label)).foregroundColor(faint)
                }
            }
            if choice.supportsEffortSlider {
                let levels = choice.supportedEfforts
                let curIdx = levels.firstIndex(of: activeEffort) ?? 0
                Slider(
                    value: Binding(
                        get: { Double(curIdx) },
                        set: { v in
                            let idx = max(0, min(levels.count - 1, Int(v.rounded())))
                            applyEffort(levels[idx], for: choice)
                        }
                    ),
                    in: 0...Double(levels.count - 1),
                    step: 1
                )
                .accessibilityLabel("\(title) effort")
                .accessibilityValue(effortName(activeEffort))
                HStack(spacing: 0) {
                    ForEach(Array(levels.enumerated()), id: \.offset) { idx, level in
                        let on = idx == curIdx
                        Button { applyEffort(level, for: choice) } label: {
                            Text(effortName(level))
                                .font(ds(on ? DS.TypeRole.labelSm : DS.TypeRole.caption))
                                .foregroundColor(on ? fg : dim)
                                .lineLimit(1)
                                .frame(maxWidth: .infinity,
                                       alignment: idx == 0 ? .leading : idx == levels.count - 1 ? .trailing : .center)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("\(effortName(level)) effort")
                    }
                }
                if let note = effortNotes[activeEffort] {
                    Text(note).font(ds(DS.TypeRole.caption)).foregroundColor(dim).fixedSize(horizontal: false, vertical: true)
                }
            } else {
                Slider(value: Binding(get: { 1.0 }, set: { _ in }), in: 0...4, step: 1)
                    .disabled(true)
                    .opacity(0.45)
                    .accessibilityLabel("\(title) effort")
                    .accessibilityValue(choice.fixedEffort.map(effortName) ?? "Not used")
                Text(choice.fixedEffort.map { "\(choice.name) uses \(effortName($0).lowercased()) effort." }
                     ?? "\(choice.name) doesn't use an effort level.")
                    .font(ds(DS.TypeRole.caption))
                    .foregroundColor(dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func applyChoice(_ choice: ModelChoice, desiredEffort: String) {
        let resolved = choice.resolve(desiredEffort: desiredEffort)
        if model.wrappedValue != resolved.modelId { model.wrappedValue = resolved.modelId }
        if choice.supportsEffortSlider || choice.fixedEffort != nil, effort.wrappedValue != resolved.effort {
            effort.wrappedValue = resolved.effort
        }
    }

    private func applyEffort(_ level: String, for choice: ModelChoice) {
        let resolved = choice.resolve(desiredEffort: level)
        if model.wrappedValue != resolved.modelId { model.wrappedValue = resolved.modelId }
        if effort.wrappedValue != resolved.effort { effort.wrappedValue = resolved.effort }
    }
}

/// Lays its children out left to right at their own size, wrapping to a new line when the next one doesn't fit
/// (the favorites' chips: each as wide as its label).
struct FlowRow: Layout {
    var spacing: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = arrange(subviews, width: proposal.width ?? .infinity)
        return CGSize(width: rows.map(\.width).max() ?? 0, height: rows.last.map { $0.y + $0.height } ?? 0)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        for row in arrange(subviews, width: bounds.width) {
            for (i, x) in zip(row.items, row.xs) {
                subviews[i].place(at: CGPoint(x: bounds.minX + x, y: bounds.minY + row.y), proposal: .unspecified)
            }
        }
    }

    private func arrange(_ subviews: Subviews, width: CGFloat) -> [(items: [Int], xs: [CGFloat], y: CGFloat, width: CGFloat, height: CGFloat)] {
        var rows = [(items: [Int], xs: [CGFloat], y: CGFloat, width: CGFloat, height: CGFloat)]()
        var cur = (items: [Int](), xs: [CGFloat](), y: CGFloat(0), width: CGFloat(0), height: CGFloat(0))
        for (i, view) in subviews.enumerated() {
            let size = view.sizeThatFits(.unspecified)
            let x = cur.items.isEmpty ? 0 : cur.width + spacing
            if !cur.items.isEmpty && x + size.width > width {
                rows.append(cur)
                cur = ([], [], cur.y + cur.height + spacing, 0, 0)
                cur.items = [i]; cur.xs = [0]; cur.width = size.width; cur.height = size.height
            } else {
                cur.items.append(i); cur.xs.append(x); cur.width = x + size.width; cur.height = max(cur.height, size.height)
            }
        }
        if !cur.items.isEmpty { rows.append(cur) }
        return rows
    }
}
