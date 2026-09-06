extends Area2D
class_name BossProjectile

@export var speed: float = 350.0
@export var damage: int = 1
@export var lifetime: float = 4.0

var direction: Vector2 = Vector2.LEFT

func _ready() -> void:
	body_entered.connect(_on_body_entered)
	area_entered.connect(_on_area_entered)
	get_tree().create_timer(lifetime).timeout.connect(queue_free)

func _physics_process(delta: float) -> void:
	position += direction * speed * delta

func set_direction(dir: Vector2) -> void:
	direction = dir.normalized()
	rotation = direction.angle()

func _on_body_entered(body: Node2D) -> void:
	if body is VoidPlayer:
		body.take_damage(damage, global_position)
		queue_free()
	elif body.is_in_group("world") or body is StaticBody2D:
		queue_free()

func _on_area_entered(area: Area2D) -> void:
	pass
