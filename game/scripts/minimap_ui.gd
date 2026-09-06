extends Control
class_name MinimapUI

@onready var location_label: Label = $Panel/LocationLabel if has_node("Panel/LocationLabel") else null
@onready var coords_label: Label = $Panel/CoordsLabel if has_node("Panel/CoordsLabel") else null
@onready var player_dot: ColorRect = $Panel/RadarFrame/PlayerDot if has_node("Panel/RadarFrame/PlayerDot") else null

var is_expanded: bool = false

func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and event.keycode == KEY_M:
		visible = !visible

func _process(_delta: float) -> void:
	var player = get_tree().get_first_node_in_group("player")
	if player:
		var scene_name = get_tree().current_scene.name if get_tree().current_scene else "Ancient Ruins"
		if location_label:
			location_label.text = "🗺️ AREA: " + scene_name.to_upper()
		if coords_label:
			var pos = player.global_position
			coords_label.text = "COORDS: X:%d Y:%d" % [int(pos.x), int(pos.y)]
		if player_dot:
			# Map relative player position into radar box (120x60)
			var rx = clamp((player.global_position.x / 1400.0) * 110.0 + 5.0, 5.0, 115.0)
			var ry = clamp((player.global_position.y / 800.0) * 50.0 + 5.0, 5.0, 55.0)
			player_dot.position = Vector2(rx, ry)
