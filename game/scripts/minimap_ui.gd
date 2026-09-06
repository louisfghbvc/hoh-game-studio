extends Control
class_name MinimapUI

@onready var location_label: Label = $Panel/LocationLabel if has_node("Panel/LocationLabel") else null
@onready var coords_label: Label = $Panel/CoordsLabel if has_node("Panel/CoordsLabel") else null

func _process(_delta: float) -> void:
	var player = get_tree().get_first_node_in_group("player")
	if player:
		var scene_name = get_tree().current_scene.name if get_tree().current_scene else "Unknown Zone"
		if location_label:
			location_label.text = "ZONE: " + scene_name.to_upper()
		if coords_label:
			var pos = player.global_position
			coords_label.text = "POS: (%d, %d)" % [int(pos.x), int(pos.y)]
