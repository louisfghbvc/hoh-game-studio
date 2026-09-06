extends Area2D
class_name BenchCheckpoint

var is_player_nearby: bool = false

func _ready() -> void:
	body_entered.connect(_on_body_entered)
	body_exited.connect(_on_body_exited)

func _unhandled_input(event: InputEvent) -> void:
	if is_player_nearby and event.is_action_pressed("ui_up"):
		rest_at_bench()

func rest_at_bench() -> void:
	var player = get_tree().get_first_node_in_group("player")
	if player:
		player.current_health = player.max_health
		player.emit_signal("health_changed", player.current_health, player.max_health)
		print("Resting at Bench: Health fully restored!")

func _on_body_entered(body: Node2D) -> void:
	if body is VoidPlayer:
		is_player_nearby = true

func _on_body_exited(body: Node2D) -> void:
	if body is VoidPlayer:
		is_player_nearby = false
