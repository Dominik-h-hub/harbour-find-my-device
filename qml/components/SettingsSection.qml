import QtQuick 2.0
import Sailfish.Silica 1.0

/*
 * Collapsible settings section: a section header that folds its content away,
 * styled to match Silica's ExpandingSection (chevron right + highlighted title
 * when open, chevron down on the right when closed).
 *
 * The open section is owned by the page (sectionId / currentSection), so the
 * sections behave like an accordion without this component knowing about its
 * siblings.
 */
Item {
    id: root

    default property alias sectionContent: contentColumn.data

    property string title
    property string sectionId
    // Set by the page to the id of the section that is currently open.
    property string currentSection
    property real buttonHeight: Theme.itemSizeMedium
    // Matches ExpandingSection: arrows sit closer to the edge than
    // horizontalPageMargin would put them.
    property real leftMargin: Theme.paddingMedium
    property real rightMargin: Theme.paddingMedium
    property int animationDuration: 200

    readonly property bool expanded: sectionId !== "" && sectionId === currentSection

    // Nothing may animate while the page is still being built: the section the
    // page starts with open would otherwise unfold itself in front of the user.
    property bool _initialized: false

    // Emitted on a header tap; the page decides which section ends up open.
    signal toggled()

    width: parent ? parent.width : 0
    height: button.height + contentContainer.height

    BackgroundItem {
        id: button

        width: parent.width
        height: contentHeight
        contentHeight: root.buttonHeight

        onClicked: root.toggled()

        Rectangle {
            anchors.fill: parent
            gradient: Gradient {
                GradientStop {
                    position: 0.0
                    color: Theme.rgba(Theme.highlightBackgroundColor, 0.1)
                }
                GradientStop {
                    position: 1.0
                    color: "transparent"
                }
            }
        }

        Icon {
            id: iconLeft

            anchors {
                right: titleLabel.left
                rightMargin: Theme.paddingSmall
                verticalCenter: parent.verticalCenter
            }
            source: "image://theme/icon-m-right"
            highlighted: button.down
            opacity: root.expanded ? 1.0 : 0.0
            Behavior on opacity { FadeAnimation { duration: root.animationDuration } }
        }

        Label {
            id: titleLabel

            text: root.title
            width: Math.min(implicitWidth,
                            parent.width - (root.expanded ? anchors.leftMargin
                                                          : anchors.rightMargin))
            anchors {
                left: undefined
                leftMargin: root.leftMargin + iconLeft.width + Theme.paddingSmall
                right: parent.right
                rightMargin: root.rightMargin + iconRight.width + Theme.paddingSmall
                verticalCenter: parent.verticalCenter
            }
            color: button.highlighted ? Theme.secondaryHighlightColor
                                      : (root.expanded ? Theme.highlightColor
                                                       : Theme.primaryColor)
            font.pixelSize: Theme.fontSizeLarge
            truncationMode: TruncationMode.Fade

            // Closed the title sits on the right like a menu entry, open it
            // moves to the left and makes room for the chevron.
            states: State {
                name: "expanded"
                when: root.expanded
                AnchorChanges {
                    target: titleLabel
                    anchors.right: undefined
                    anchors.left: parent.left
                }
            }
            transitions: Transition {
                enabled: root._initialized
                AnchorAnimation { duration: root.animationDuration }
            }
        }

        Icon {
            id: iconRight

            anchors {
                right: parent.right
                rightMargin: root.rightMargin
                verticalCenter: parent.verticalCenter
            }
            source: "image://theme/icon-m-down"
            highlighted: button.down
            opacity: root.expanded ? 0.0 : 1.0
            Behavior on opacity { FadeAnimation { duration: root.animationDuration } }
        }
    }

    Item {
        id: contentContainer

        anchors.top: button.bottom
        width: parent.width
        height: root.expanded ? contentColumn.height : 0
        opacity: root.expanded ? 1.0 : 0.0
        // Only while folding: clipping a fully open section would cut off
        // anything a child draws outside its own bounds.
        clip: height < contentColumn.height
        // Zero height and zero opacity do NOT stop input in Qt Quick, so a
        // closed section would keep swallowing taps meant for the ones below.
        visible: height > 0

        // Enabled around an expand/collapse only. Left on permanently it would
        // also animate every content height change -- a growing TextField, a
        // status label switching lines -- which reads as lag, not as motion.
        Behavior on height {
            id: expandBehavior
            enabled: false
            NumberAnimation {
                id: expandAnim
                duration: root.animationDuration
                onRunningChanged: if (!running) expandBehavior.enabled = false
            }
        }
        Behavior on opacity { FadeAnimation { duration: root.animationDuration } }

        Column {
            id: contentColumn
            width: parent.width
            spacing: Theme.paddingSmall
        }
    }

    Component.onCompleted: _initialized = true

    onExpandedChanged: expandBehavior.enabled = _initialized
}
