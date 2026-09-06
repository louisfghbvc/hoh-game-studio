extends StaticBody2D
class_name BossArenaDoor

@export var is_closed: bool = false

@onready var collision_shape: CollisionShape2D = $CollisionShape2D if has_node("CollisionShape2D") else null
@onready var visual_rect: ColorRect = $ColorRect if has_node("ColorRect") else null

func _ready() -> void:
	update_door_state()

func close_door() -> void:
	is_closed = true
	update_door_state()

func open_door() -> void:
	is_closed = false
	update_door_state()

func update_door_state() -> void:
	if collision_shape:
		collision_shape.disabled = not is_closed
	if visual_rect:
		visual_rect.visible = is_closed
