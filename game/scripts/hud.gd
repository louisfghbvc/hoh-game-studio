extends CanvasLayer
class_name GameHUD

@onready var health_label: Label = $MarginContainer/VBoxContainer/HealthLabel if has_node("MarginContainer/VBoxContainer/HealthLabel") else null
@onready var soul_progress: ProgressBar = $MarginContainer/VBoxContainer/SoulBar if has_node("MarginContainer/VBoxContainer/SoulBar") else null

func _ready() -> void:
	var player = get_tree().get_first_node_in_group("player")
	if player:
		player.health_changed.connect(_on_player_health_changed)
		player.soul_changed.connect(_on_player_soul_changed)
		_on_player_health_changed(player.current_health, player.max_health)
		_on_player_soul_changed(player.current_soul, player.max_soul)

func _on_player_health_changed(current_hp: int, max_hp: int) -> void:
	if health_label:
		var masks = "🤍 ".repeat(current_hp)
		var empty_masks = "🖤 ".repeat(max_hp - current_hp)
		health_label.text = "MASKS: " + masks + empty_masks

func _on_player_soul_changed(current_soul: float, max_soul: float) -> void:
	if soul_progress:
		soul_progress.value = (current_soul / max_soul) * 100.0
