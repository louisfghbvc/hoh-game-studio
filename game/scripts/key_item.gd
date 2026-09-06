extends Area2D
class_name KeyItem

@export var key_id: String = "ancient_key"
@export var key_name: String = "Ancient Key"

signal key_collected(key_id: String)

func _ready() -> void:
	body_entered.connect(_on_body_entered)

func _on_body_entered(body: Node2D) -> void:
	if body is VoidPlayer or body.is_in_group("player"):
		if body.has_method("add_key"):
			body.add_key(key_id)
		emit_signal("key_collected", key_id)
		queue_free()
