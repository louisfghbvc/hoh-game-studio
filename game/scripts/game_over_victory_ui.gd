extends CanvasLayer
class_name GameOverVictoryUI

@onready var banner_label: Label = $CenterContainer/VBoxContainer/BannerLabel if has_node("CenterContainer/VBoxContainer/BannerLabel") else null
@onready var restart_button: Button = $CenterContainer/VBoxContainer/RestartButton if has_node("CenterContainer/VBoxContainer/RestartButton") else null

func _ready() -> void:
	visible = false
	if restart_button:
		restart_button.pressed.connect(_on_restart_pressed)

	var player = get_tree().get_first_node_in_group("player")
	if player:
		player.health_changed.connect(_on_player_health_changed)

func show_victory() -> void:
	visible = true
	if banner_label:
		banner_label.text = "🏆 DEMO COMPLETED!\nGUARDIAN BOSS DEFEATED"
		banner_label.modulate = Color(0.2, 0.9, 0.4)

func show_game_over() -> void:
	visible = true
	if banner_label:
		banner_label.text = "💀 MASK BROKEN\nGAME OVER"
		banner_label.modulate = Color(0.9, 0.2, 0.2)

func _on_player_health_changed(current_hp: int, max_hp: int) -> void:
	if current_hp <= 0:
		show_game_over()

func _on_restart_pressed() -> void:
	get_tree().reload_current_scene()
