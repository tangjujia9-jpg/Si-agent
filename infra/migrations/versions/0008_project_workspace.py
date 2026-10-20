"""Associate projects with an explicit folder or an imported folder snapshot."""

from alembic import op

revision = "0008_project_workspace"
down_revision = "0007_embedding_dimensions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE projects ADD COLUMN root_path TEXT")
    op.execute("ALTER TABLE projects ADD COLUMN folder_name VARCHAR(160) NOT NULL DEFAULT ''")


def downgrade():
    op.drop_column("projects", "folder_name")
    op.drop_column("projects", "root_path")
