extends VoidEnemy
class_name CrawlerEnemy

@export var move_speed: float = 80.0
var direction: int = 1
var gravity: float = ProjectSettings.get_setting("physics/2d/default_gravity", 980.0)

@onready var ledge_check: RayCast2D = $LedgeCheck if has_node("LedgeCheck") else null

func _physics_process(delta: float) -> void:
	if not is_on_floor():
		velocity.y += gravity * delta
	
	# Patrol movement
	velocity.x = direction * move_speed
	
	# Turn around at wall or ledge edge
	if is_on_wall() or (ledge_check and is_on_floor() and not ledge_check.is_colliding()):
		direction *= -1
		if ledge_check:
			ledge_check.position.x *= -1

	move_and_slide()
