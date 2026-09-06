extends Area2D
class_name SpikesHazard

@export var damage: int = 1

func _ready() -> void:
	body_entered.connect(_on_body_entered)

func _on_body_entered(body: Node2D) -> void:
	if body is VoidPlayer:
		body.take_damage(damage, global_position)
		# Reset player to safe platform
		body.global_position = Vector2(300, 450)
