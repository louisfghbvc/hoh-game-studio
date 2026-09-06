extends CanvasLayer
class_name GameHUD

@onready var masks_container: HBoxContainer = $Control/TopLeftFrame/VBoxContainer/MasksContainer if has_node("Control/TopLeftFrame/VBoxContainer/MasksContainer") else null
@onready var soul_orb_fill: ColorRect = $Control/TopLeftFrame/SoulVesselFrame/SoulFill if has_node("Control/TopLeftFrame/SoulVesselFrame/SoulFill") else null
@onready var soul_label: Label = $Control/TopLeftFrame/SoulVesselFrame/SoulPercentLabel if has_node("Control/TopLeftFrame/SoulVesselFrame/SoulPercentLabel") else null
@onready var inventory_label: Label = $Control/TopRightFrame/InventoryLabel if has_node("Control/TopRightFrame/InventoryLabel") else null

var mask_panels: Array[PanelContainer] = []

func _ready() -> void:
	var player = get_tree().get_first_node_in_group("player")
	if player:
		player.health_changed.connect(_on_player_health_changed)
		player.soul_changed.connect(_on_player_soul_changed)
		_setup_masks(player.max_health)
		_on_player_health_changed(player.current_health, player.max_health)
		_on_player_soul_changed(player.current_soul, player.max_soul)

func _setup_masks(max_hp: int) -> void:
	if not masks_container:
		return
	# Clear old children
	for c in masks_container.get_children():
		c.queue_free()
	mask_panels.clear()

	for i in range(max_hp):
		var p = PanelContainer.new()
		p.custom_minimum_size = Vector2(28, 36)
		
		var style = StyleBoxFlat.new()
		style.bg_color = Color(0.92, 0.95, 1.0, 1.0) # White Mask
		style.corner_radius_top_left = 12
		style.corner_radius_top_right = 12
		style.corner_radius_bottom_left = 6
		style.corner_radius_bottom_right = 6
		style.border_width_left = 2
		style.border_width_top = 2
		style.border_width_right = 2
		style.border_width_bottom = 2
		style.border_color = Color(0.1, 0.15, 0.2, 1.0)
		p.add_theme_stylebox_override("panel", style)

		# Add inner eyes detail
		var label = Label.new()
		label.text = "⚫"
		label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
		label.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
		label.add_theme_font_size_override("font_size", 10)
		p.add_child(label)

		masks_container.add_child(p)
		mask_panels.append(p)

func _on_player_health_changed(current_hp: int, max_hp: int) -> void:
	if mask_panels.size() != max_hp:
		_setup_masks(max_hp)

	for i in range(max_hp):
		var p = mask_panels[i]
		var style = p.get_theme_stylebox("panel") as StyleBoxFlat
		if style:
			if i < current_hp:
				style.bg_color = Color(0.95, 0.96, 1.0, 1.0) # Active mask
				style.border_color = Color(0.15, 0.2, 0.3, 1.0)
				p.modulate = Color(1, 1, 1, 1)
			else:
				style.bg_color = Color(0.15, 0.15, 0.2, 0.6) # Broken / Empty mask
				style.border_color = Color(0.4, 0.2, 0.2, 0.8)
				p.modulate = Color(0.5, 0.5, 0.5, 0.7)

func _on_player_soul_changed(current_soul: float, max_soul: float) -> void:
	var ratio = clamp(current_soul / max_soul, 0.0, 1.0)
	if soul_orb_fill:
		soul_orb_fill.scale.y = ratio
	if soul_label:
		soul_label.text = str(int(ratio * 100)) + "%"
